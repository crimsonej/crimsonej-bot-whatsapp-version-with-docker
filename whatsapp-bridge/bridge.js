require('dotenv').config();
// Optional: allow disabling TLS certificate validation for debugging transient EPROTO
// Set ALLOW_INSECURE_TLS=1 in Space secrets only as a temporary debug measure.
if (process.env.ALLOW_INSECURE_TLS === '1') {
    process.env.NODE_TLS_REJECT_UNAUTHORIZED = '0';
    console.warn('[BRIDGE] WARNING: TLS certificate validation disabled (ALLOW_INSECURE_TLS=1)');
}
// Diagnostic info to aid TLS/WebSocket debugging
try {
    console.log('[DIAG] Node version:', process.version);
    console.log('[DIAG] OpenSSL version:', process.versions.openssl);
    console.log('[DIAG] NODE_OPTIONS:', process.env.NODE_OPTIONS || '<unset>');
} catch (e) { console.log('[DIAG] diag error', e && e.message); }
const {
    default: makeWASocket,
    useMultiFileAuthState,
    DisconnectReason,
    downloadMediaMessage,
    fetchLatestBaileysVersion,
} = require('@whiskeysockets/baileys');
const qrcode = require('qrcode-terminal');
const axios  = require('axios');
const { Boom } = require('@hapi/boom');
const fs   = require('fs');
const path = require('path');
const os   = require('os');

// Prefer IPv4 addresses first to avoid TLS errors on hosts with unroutable IPv6
try { require('dns').setDefaultResultOrder('ipv4first'); } catch (_) {}

const AI_SERVER = process.env.AI_SERVER || 'http://localhost:5000/reply';
const BOT_NAME  = process.env.BOT_NAME  || 'crimsonej';
// Use /data (HF persistent storage) when available, otherwise local fallback
const AUTH_DIR  = process.env.AUTH_DIR  || (fs.existsSync('/data') ? '/data/auth_info_baileys' : 'auth_info_baileys');

let sock = null;
const seenContacts = new Set();

// ── Self-awareness observability ─────────────────────────────────────────────
let lastActivityAt = Date.now();
let lastSendAt = null;
let sendCount = 0;
let receiveCount = 0;
let lastErrorAt = null;
let lastErrorMsg = null;
const recentEvents = [];                 // ring buffer, max 30

function recordEvent(kind, summary) {
    lastActivityAt = Date.now();
    recentEvents.push({ ts: lastActivityAt, kind, summary });
    if (recentEvents.length > 30) recentEvents.shift();
}

async function instrumentedSend(jid, content, options) {
    try {
        const res = await sock.sendMessage(jid, content, options);
        sendCount++;
        lastSendAt = Date.now();
        const kind = typeof content === 'object' ? Object.keys(content)[0] : 'text';
        recordEvent('send', `${kind} -> ${jid.split('@')[0]}`);
        return res;
    } catch (e) {
        lastErrorAt = Date.now();
        lastErrorMsg = e && e.message;
        recordEvent('send_fail', `${e && e.message} -> ${jid.split('@')[0]}`);
        throw e;
    }
}

// Auto-fallback: disable TLS verification after repeated EPROTO failures (debug only)
let eprotoCount = 0;
const EPROTO_THRESHOLD = 3;
const https = require('https');

// Helper: defensively normalize JIDs and extract user portion when domains
// like @lid appear. Returns a safe JID string (e.g. '250203957407887@s.whatsapp.net')
function normalizeJid(sender) {
    if (!sender || typeof sender !== 'string') return null;
    try {
        // Strip device id after ':' and keep the main addr
        const base = sender.split(':')[0];
        const parts = base.split('@');
        const user = parts[0] || '';
        const domain = parts[1] || '';
        if (!user) return null;
        // If domain looks like a WhatsApp domain, keep it; otherwise map to s.whatsapp.net
        const safeDomain = domain && domain.includes('whatsapp') ? domain : 's.whatsapp.net';
        return `${user}@${safeDomain}`;
    } catch (e) {
        return null;
    }
}

// ─── Boot ────────────────────────────────────────────────────────────────────
async function startBot() {
    const { state, saveCreds } = await useMultiFileAuthState(AUTH_DIR);
    const { version } = await fetchLatestBaileysVersion();

    sock = makeWASocket({
        version,
        auth: state,
        browser: ['Ubuntu', 'Chrome', '22.04'],
        generateHighQualityLinkPreview: false,
        syncFullHistory: false,
        printQRInTerminal: false,
        connectTimeoutMs: 60000,
        defaultQueryTimeoutMs: 60000,
        keepAliveIntervalMs: 10000,
        getMessage: async (key) => {
            return { conversation: 'Hello' };
        }
    });

    // ── Connection lifecycle ─────────────────────────────────────────────────
    sock.ev.on('connection.update', (update) => {
        const { connection, lastDisconnect, qr } = update;

        if (qr) {
            console.log('\n\n======== SCAN THIS QR CODE ========');
            qrcode.generate(qr, { small: true });
            console.log('====================================\n');
            recordEvent('qr', 'qr_code_emitted');
        }

        if (connection === 'close') {
            const code = (lastDisconnect?.error instanceof Boom)
                ? lastDisconnect.error.output?.statusCode
                : (lastDisconnect?.error?.data?.code || null);
            const loggedOut = code === DisconnectReason.loggedOut;
            console.log(`[BRIDGE] Closed – code ${code} | loggedOut: ${loggedOut}`);
            // Detect TLS EPROTO failures and auto-fallback if repeated
            const errCode = lastDisconnect?.error?.data?.code || lastDisconnect?.error?.data?.errno || '';
            const errMsg = lastDisconnect?.error?.message || '';
            if (String(errCode).toUpperCase().includes('EPROTO') || String(errMsg).toUpperCase().includes('EPROTO')) {
                eprotoCount++;
                console.warn(`[BRIDGE] Detected EPROTO TLS failure (count=${eprotoCount})`);
                if (eprotoCount >= EPROTO_THRESHOLD && process.env.NODE_TLS_REJECT_UNAUTHORIZED !== '0') {
                    console.warn('[BRIDGE] Reached EPROTO threshold — disabling TLS certificate validation (debug)');
                    process.env.NODE_TLS_REJECT_UNAUTHORIZED = '0';
                    try { https.globalAgent.options.rejectUnauthorized = false; } catch (_) {}
                    // restart connection with insecure TLS
                    try { sock.ws?.terminate?.(); } catch (_) {}
                    setTimeout(startBot, 2000);
                    return;
                }
            }
            if (loggedOut) {
                console.log('[BRIDGE] Logged out. Auto-deleting auth folder to generate a fresh QR code...');
                try {
                    fs.rmSync(AUTH_DIR, { recursive: true, force: true });
                } catch (err) {
                    console.error('[BRIDGE] Failed to delete auth folder:', err);
                }
                setTimeout(() => process.exit(1), 1000);
            } else {
                console.log('[BRIDGE] Reconnecting in 5s...');
                setTimeout(startBot, 5000);
            }
        } else if (connection === 'open') {
            const botId  = sock.user?.id || '';
            const botNum = botId.split(':')[0].split('@')[0];
            console.log(`[BRIDGE] Ready! Bot number: ${botNum}`);
            recordEvent('connection_open', `bot=${botNum}`);
        }
    });

    sock.ev.on('creds.update', saveCreds);

    // ── Incoming messages ────────────────────────────────────────────────────
    sock.ev.on('messages.upsert', async ({ messages, type }) => {
        if (type !== 'notify') return;
        for (const msg of messages) {
            receiveCount++;
            const mtype = msg.message && Object.keys(msg.message)[0] || 'unknown';
            recordEvent('receive', `${mtype} from ${(msg.key?.remoteJid || '').split('@')[0]}`);
            await handleMessage(msg).catch(err =>
                console.error('[BRIDGE] Handler error:', err.message)
            );
        }
    });

    // ── Incoming message EDITS ───────────────────────────────────────────────
    // The `messages.update` event is shared with read-receipts, reactions,
    // REVOKE, group deletions, and the local "I just sent" case. The single
    // check on `update.message?.editedMessage` is enough to filter to edits.
    sock.ev.on('messages.update', async (updates) => {
        for (const u of updates || []) {
            if (u?.key?.fromMe) continue;
            const edit = u?.update?.message?.editedMessage;
            if (!edit) continue;
            const inner = edit.message || {};
            const newText = inner.conversation
                          || inner.extendedTextMessage?.text
                          || inner.imageMessage?.caption
                          || inner.videoMessage?.caption
                          || inner.documentMessage?.caption
                          || '';
            const jid = u.key?.remoteJid || '';
            const participant = u.key?.participant || '';
            const messageId = u.key?.id || '';
            if (!jid || !messageId) continue;
            console.log(`[EDIT] jid=${jid.split('@')[0]} from=${participant.split('@')[0]} id=${messageId} new_text=${JSON.stringify(newText)}`);
            recordEvent('edit', `${participant.split('@')[0]} -> ${newText.slice(0, 40)}`);
            // Forward to Flask /reply with edited=true so it can swap the
            // last user turn in the session before re-running the LLM.
            try {
                await axios.post(AI_SERVER, {
                    message: newText || '[edited empty]',
                    edited: true,
                    message_id: messageId,
                    sender: participant || jid,
                    user_phone: (participant || jid).split(':')[0].split('@')[0],
                    group_name: jid.endsWith('@g.us') ? jid : null,
                }, { timeout: 5 }).catch(err => {
                    console.error('[EDIT] forward failed:', err.message);
                });
            } catch (err) {
                console.error('[EDIT] unexpected:', err.message);
            }
        }
    });
}

// ─── Message Handler ─────────────────────────────────────────────────────────
async function handleMessage(msg) {
    if (!msg.message || msg.key.fromMe) return;

    const pushName = msg.pushName || '';
    const from = msg.key.remoteJid;
    const isGroup = from && from.endsWith('@g.us');
    const senderJid = isGroup ? (msg.key.participant || '') : from;
    if (senderJid && senderJid.endsWith('@s.whatsapp.net')) {
        seenContacts.add(senderJid);
    }

    if (from === 'status@broadcast') {
        // Auto-view the status
        await sock.readMessages([msg.key]).catch(e => console.error('[BRIDGE] readMessages error:', e.message));

        const senderJid = msg.key.participant || '';
        if (senderJid && senderJid.endsWith('@s.whatsapp.net')) {
            seenContacts.add(senderJid);
        } else {
            return;
        }
        const userPhone = senderJid.split(':')[0].split('@')[0];

        // Bot identity
        const botJid = sock.user?.id || '';
        const botNum = botJid.split(':')[0].split('@')[0];
        const botLid = sock.user?.lid ? sock.user.lid.split(':')[0].split('@')[0] : '';
        if (userPhone === botNum) return;

        const m = msg.message;
        const extM = m.extendedTextMessage;
        const text = m.conversation
                  || extM?.text
                  || m.imageMessage?.caption
                  || m.videoMessage?.caption
                  || '';

        let imageData = null;
        if (m.imageMessage) {
            const buf = await downloadMediaMessage(msg, 'buffer', {}).catch(() => null);
            if (buf) imageData = buf.toString('base64');
        }

        if (text || imageData) {
            console.log(`[STATUS] Received status from ${userPhone}: ${text || '[Image]'}`);
            try {
                const res = await axios.post(AI_SERVER, {
                    message: text || '[Image]',
                    image_base64: imageData,
                    is_status: true,
                    sender: from,
                    user_phone: userPhone,
                    push_name: pushName,
                    bot_id: botNum,
                    bot_lid: botLid
                });
                if (res?.data && res.data.reply) {
                    const quotedFake = {
                        message: m,
                        key: {
                            remoteJid: 'status@broadcast',
                            participant: senderJid,
                            id: msg.key.id
                        }
                    };
                    await instrumentedSend(senderJid, { text: res.data.reply }, { quoted: quotedFake });
                    console.log(`[STATUS] Replied to status of ${userPhone}: ${res.data.reply}`);
                }
            } catch (e) {
                console.error('[BRIDGE] Status reply error:', e.message);
            }
        }
        return;
    }

    // Mark message as read (blue ticks)
    await sock.readMessages([msg.key]).catch(e => console.error('[BRIDGE] readMessages error:', e.message));

    // Bot identity
    const botJid = sock.user?.id || '';
    const botNum = botJid.split(':')[0].split('@')[0];
    const botLid = sock.user?.lid ? sock.user.lid.split(':')[0].split('@')[0] : '';

    // Sender info
    const userPhone = senderJid.split(':')[0].split('@')[0];

    // Prevent infinite loop: do not process messages sent from the bot's own number to itself
    if (userPhone === botNum) return;

    // Unwrap message content layers
    const m    = msg.message;
    const extM = m.extendedTextMessage;
    const ctx  = extM?.contextInfo
              || m.imageMessage?.contextInfo
              || m.videoMessage?.contextInfo
              || m.audioMessage?.contextInfo
              || m.stickerMessage?.contextInfo
              || m.documentMessage?.contextInfo
              || {};

    let text = m.conversation
              || extM?.text
              || m.imageMessage?.caption
              || m.videoMessage?.caption
              || '';

    // Media type flags
    const hasImage   = !!m.imageMessage;
    const hasVideo   = !!m.videoMessage;
    const hasAudio   = !!m.audioMessage;
    const hasSticker = !!m.stickerMessage;
    const hasDocument = !!m.documentMessage;

    // Quoted message helpers
    const quotedMsg    = ctx.quotedMessage   || null;
    const isReply      = !!quotedMsg;
    const quotedSender = isReply ? (ctx.participant || ctx.remoteJid) : null;
    const quotedText   = quotedMsg?.conversation
                      || quotedMsg?.extendedTextMessage?.text
                      || '';

    // Helper: build a fake msg object for downloadMediaMessage and quoting
    const quotedFake = quotedMsg ? { message: quotedMsg, key: { remoteJid: from, participant: quotedSender, id: ctx.stanzaId || '' } } : null;

    console.log(`[DEBUG] ${isGroup ? 'Group' : 'DM'} from ${userPhone}: ${text || '[media]'}`);

    // ── Group filter ─────────────────────────────────────────────────────────
    let replyToQuoted = false;

    const mentionedJid = ctx.mentionedJid || [];
    const isMentioned = mentionedJid.includes(sock.user?.id) || mentionedJid.some(j => {
        const num = j.split(':')[0].split('@')[0];
        return num === botNum || (botLid && num === botLid);
    });
    const nameMentioned = text.toLowerCase().includes(BOT_NAME.toLowerCase());
    
    const isReplyToBot = isReply && (() => {
        if (!quotedSender) return false;
        // Strip device suffix and @domain, e.g. "256741125387:27@s.whatsapp.net" → "256741125387"
        const qs = quotedSender.split(':')[0].split('@')[0];
        const match = qs === botNum || (botLid && qs === botLid) || quotedSender === sock.user?.id;
        console.log(`[DEBUG] quotedSender=${quotedSender} qs=${qs} botNum=${botNum} botLid=${botLid} match=${match}`);
        return match;
    })();

    if (isGroup) {
        const isCommand = text.startsWith('/');
        if (isCommand) {
            if (text.trim().startsWith('/respond') && isReply && !isReplyToBot) {
                text = text.replace(/^\/respond\s*/, '');
                replyToQuoted = true;
            }
        } else {
            console.log(`[DEBUG] isBotMentioned=${isMentioned} nameMentioned=${nameMentioned} isReplyToBot=${isReplyToBot}`);
            if (!isMentioned && !nameMentioned && !isReplyToBot) return;
        }
    } else {
        if (text.trim().startsWith('/respond') && isReply && !isReplyToBot) {
            text = text.replace(/^\/respond\s*/, '');
            replyToQuoted = true;
        }
    }

    // ── Received sticker → AI analysis ──────────────────────────────────────
    if (hasSticker && !text.startsWith('/')) {
        let process = !isGroup;
        if (isGroup && quotedSender) {
            process = quotedSender === sock.user?.id || quotedSender.split(':')[0].split('@')[0] === botNum || (botLid && quotedSender.split(':')[0].split('@')[0] === botLid);
        }
        if (process) {
            const buf = await downloadMediaMessage(msg, 'buffer', {}).catch(() => null);
            if (buf) {
                try {
                    await sock.sendPresenceUpdate('composing', from).catch(() => {});
                    const res = await axios.post(AI_SERVER, {
                        sticker: true,
                        sticker_data: buf.toString('base64'),
                        sticker_mimetype: 'image/webp',
                        sender: from,
                        user_phone: userPhone,
                        is_reply_to_bot: isReplyToBot,
                        quoted_author: quotedSender
                    });
                    await sendAIResponse(msg, from, res, quotedFake, quotedSender);
                } catch (e) { console.error('[Sticker]', e.message); }
            }
        }
        return;
    }

    // ── /read command ─────────────────────────────────────────────────────────
    // Usage: send a doc with caption "/read [optional prompt]"
    //        OR reply to a doc message with "/read [optional prompt]"
    const isReadCmd = text.startsWith('/read');
    let docBuf = null, docName = '', docMime = '';

    if (isReadCmd) {
        if (hasDocument) {
            docBuf  = await downloadMediaMessage(msg, 'buffer', {}).catch(() => null);
            docName = m.documentMessage.fileName || 'document';
            docMime = m.documentMessage.mimetype || '';
        } else if (quotedFake && quotedMsg?.documentMessage) {
            docBuf  = await downloadMediaMessage(quotedFake, 'buffer', {}).catch(() => null);
            docName = quotedMsg.documentMessage.fileName || 'document';
            docMime = quotedMsg.documentMessage.mimetype || '';
        }

        if (!docBuf) {
            await instrumentedSend(from, { text: '📄 Please send a document with /read, or reply to a document with /read.' }, { quoted: msg });
            return;
        }

        const userPrompt = text.replace(/^\/read\s*/i, '').trim();
        await sock.sendPresenceUpdate('composing', from).catch(() => {});
        try {
            const res = await axios.post(AI_SERVER, {
                document:           true,
                document_data:      docBuf.toString('base64'),
                document_name:      docName,
                document_mimetype:  docMime,
                message:            userPrompt || '',
                sender:             from,
                user_phone:         userPhone,
                is_group:           isGroup,
                read_command:       true,
            });
            await sendAIResponse(msg, from, res, quotedFake, quotedSender);
        } catch (e) { console.error('[/read]', e.message); }
        return;
    }

    // ── /learn command ────────────────────────────────────────────────────────
    // Usage: send a doc or text with caption "/learn"
    //        OR reply to a doc or text message with "/learn"
    const isLearnCmd = text.startsWith('/learn');
    if (isLearnCmd) {
        let lBuf = null, lName = '', lMime = '';
        let lText = text.replace(/^\/learn\s*/i, '').trim();

        if (hasDocument) {
            lBuf  = await downloadMediaMessage(msg, 'buffer', {}).catch(() => null);
            lName = m.documentMessage.fileName || 'document';
            lMime = m.documentMessage.mimetype || '';
        } else if (quotedFake && quotedMsg?.documentMessage) {
            lBuf  = await downloadMediaMessage(quotedFake, 'buffer', {}).catch(() => null);
            lName = quotedMsg.documentMessage.fileName || 'document';
            lMime = quotedMsg.documentMessage.mimetype || '';
        } else if (quotedFake && quotedText) {
            lText = quotedText;
        }

        if (!lBuf && !lText) {
            await instrumentedSend(from, { text: '🧠 Please send or reply to a document/text with /learn to add it to my permanent memory.' }, { quoted: msg });
            return;
        }

        await sock.sendPresenceUpdate('composing', from).catch(() => {});
        try {
            const res = await axios.post(AI_SERVER, {
                document:           !!lBuf,
                document_data:      lBuf ? lBuf.toString('base64') : null,
                document_name:      lName,
                document_mimetype:  lMime,
                message:            lText,
                sender:             from,
                user_phone:         userPhone,
                is_group:           isGroup,
                learn_command:      true,
            });
            await sendAIResponse(msg, from, res, quotedFake, quotedSender);
        } catch (e) { console.error('[/learn]', e.message); }
        return;
    }

    // ── Passive document auto-detect (non-/read) ─────────────────────────────
    if (hasDocument) {
        const docMsg   = m.documentMessage;
        const fileName = docMsg.fileName || 'document';
        const mimetype = docMsg.mimetype || '';
        const allowed  = ['application/pdf', 'application/vnd.openxmlformats-officedocument.wordprocessingml.document', 'text/plain'];
        if (allowed.includes(mimetype) || fileName.endsWith('.pdf') || fileName.endsWith('.docx') || fileName.endsWith('.txt')) {
            const buf = await downloadMediaMessage(msg, 'buffer', {}).catch(() => null);
            if (buf) {
                try {
                    await sock.sendPresenceUpdate('composing', from).catch(() => {});
                    const res = await axios.post(AI_SERVER, {
                        document:          true,
                        document_data:     buf.toString('base64'),
                        document_name:     fileName,
                        document_mimetype: mimetype,
                        message:           text || '',
                        sender:            from,
                        user_phone:        userPhone,
                        is_group:          isGroup,
                        read_command:      false,
                    });
                    await sendAIResponse(msg, from, res, quotedFake, quotedSender);
                } catch (e) { console.error('[Document]', e.message); }
                return;
            }
        }
    }


    // ── /reg-img ─────────────────────────────────────────────────────────────
    if (text.startsWith('/reg-img')) {
        let imgBuf  = null;
        let imgMime = 'image/jpeg';

        if (hasImage) {
            imgBuf  = await downloadMediaMessage(msg, 'buffer', {}).catch(() => null);
            imgMime = m.imageMessage?.mimetype || 'image/jpeg';
        } else if (quotedFake && quotedMsg?.imageMessage) {
            imgBuf  = await downloadMediaMessage(quotedFake, 'buffer', {}).catch(() => null);
            imgMime = quotedMsg.imageMessage?.mimetype || 'image/jpeg';
        }

        if (!imgBuf) {
            await instrumentedSend(from, { text: 'Please send an image or reply to one with /reg-img' }, { quoted: msg });
            return;
        }
        try {
            await sock.sendPresenceUpdate('composing', from).catch(() => {});
            const res = await axios.post(AI_SERVER, {
                message: text,
                image_base64: imgBuf.toString('base64'),
                mime_type: imgMime,
                sender: from,
                user_phone: userPhone,
            });
            await sendAIResponse(msg, from, res, quotedFake, quotedSender);
        } catch (e) {
            await instrumentedSend(from, { text: 'Sorry, I could not analyze that image.' }, { quoted: msg });
        }
        return;
    }

    // ── /sticker ─────────────────────────────────────────────────────────────
    if (text.startsWith('/sticker') || text === '/sticker') {
        let mediaBuf  = null;
        let mediaType = null;

        if (hasImage) {
            mediaBuf  = await downloadMediaMessage(msg, 'buffer', {}).catch(() => null);
            mediaType = 'image';
        } else if (hasVideo) {
            mediaBuf  = await downloadMediaMessage(msg, 'buffer', {}).catch(() => null);
            mediaType = 'video';
        } else if (hasSticker) {
            // stickers are already webp; download and send back
            mediaBuf  = await downloadMediaMessage(msg, 'buffer', {}).catch(() => null);
            mediaType = 'sticker';
        } else if (quotedFake && quotedMsg?.imageMessage) {
            mediaBuf  = await downloadMediaMessage(quotedFake, 'buffer', {}).catch(() => null);
            mediaType = 'image';
        } else if (quotedFake && quotedMsg?.videoMessage) {
            mediaBuf  = await downloadMediaMessage(quotedFake, 'buffer', {}).catch(() => null);
            mediaType = 'video';
        } else if (quotedFake && quotedMsg?.stickerMessage) {
            mediaBuf  = await downloadMediaMessage(quotedFake, 'buffer', {}).catch(() => null);
            mediaType = 'sticker';
        }

        if (!mediaBuf) {
            await instrumentedSend(from, { text: 'Please send or reply to an image/video with /sticker' }, { quoted: msg });
            return;
        }

        await sock.sendPresenceUpdate('composing', from).catch(() => {});

        if (mediaType === 'image') {
            const sharp = require('sharp');
            const webp  = await sharp(mediaBuf).webp().toBuffer();
            await instrumentedSend(from, { sticker: webp });
        } else if (mediaType === 'sticker') {
            // already webp, send directly
            await instrumentedSend(from, { sticker: mediaBuf });
        } else if (mediaType === 'video') {
            const { exec } = require('child_process');
            const execP    = require('util').promisify(exec);
            const orig     = path.join(os.tmpdir(), `orig_${Date.now()}.mp4`);
            const out      = path.join(os.tmpdir(), `stk_${Date.now()}.webp`);
            fs.writeFileSync(orig, mediaBuf);
            try {
                await execP(`ffmpeg -i "${orig}" -t 6 -vf "fps=10,scale=512:512:force_original_aspect_ratio=decrease,pad=512:512:(ow-iw)/2:(oh-ih)/2" -c:v libwebp -quality 70 -loop 0 -an "${out}" -y`);
                await instrumentedSend(from, { sticker: fs.readFileSync(out) });
            } catch (e) {
                await instrumentedSend(from, { text: "Couldn't convert that video to a sticker." }, { quoted: msg });
            } finally {
                [orig, out].forEach(p => { try { fs.unlinkSync(p); } catch (_) {} });
            }
        }
        return;
    }

    // ── Vision auto-attach ───────────────────────────────────────────────────
    let imageData = null;
    if (hasImage || hasSticker) {
        const buf = await downloadMediaMessage(msg, 'buffer', {}).catch((e) => {
            console.log(`[MEDIA] Download failed: ${e.message}`);
            return null;
        });
        console.log(`[MEDIA] hasImage=${hasImage} hasSticker=${hasSticker} downloaded=${!!buf}`);
        if (buf) imageData = buf.toString('base64');
    } else if (quotedFake && (quotedMsg?.imageMessage || quotedMsg?.stickerMessage)) {
        const buf = await downloadMediaMessage(quotedFake, 'buffer', {}).catch(() => null);
        if (buf) imageData = buf.toString('base64');
    }

    try {
        // Show "typing..." indicator immediately and keep it active
        await sock.sendPresenceUpdate('composing', from).catch(() => {});
        const typingInterval = setInterval(() => {
            sock.sendPresenceUpdate('composing', from).catch(() => {});
        }, 10000); // Refresh every 10s

        const res = await axios.post(AI_SERVER, {
            message:        text || '[media]',
            quoted_message: quotedText || null,
            reply_to_quoted: replyToQuoted,
            image_data:     imageData,
            image_base64:   imageData,
            sender:         from,
            user_phone:     userPhone,
            push_name:      pushName,
            group_name:     isGroup ? from : null,
            bot_id:         botNum,
            bot_lid:        botLid
        });
        
        clearInterval(typingInterval);
        await sendAIResponse(msg, from, res, quotedFake, quotedSender);
    } catch (e) {
        console.error('[BRIDGE] AI error:', e.message);
    }
}

// ─── AI Response Dispatcher ──────────────────────────────────────────────────
async function sendAIResponse(originalMsg, from, response, quotedFake, quotedAuthor) {
    if (!response?.data) return;
    const data = response.data;

    // Collect message IDs for self-correction
    const sentMessageIds = [];
    let sentText = '';

    // Helper to wrap instrumentedSend and capture message_id
    const trackedSend = async (jid, content, options) => {
        const res = await instrumentedSend(jid, content, options);
        if (res?.key?.id) {
            sentMessageIds.push(res.key.id);
        }
        return res;
    };

    // Send file attachments
    const sendFile = async (filePath, type, filename) => {
        if (!filePath || !fs.existsSync(filePath)) return;
        try {
            const buf = fs.readFileSync(filePath);
            if      (type === 'audio') await trackedSend(from, { audio: buf, mimetype: 'audio/ogg; codecs=opus', ptt: data.ptt || false });
            else if (type === 'video') await trackedSend(from, { video: buf, mimetype: 'video/mp4', fileName: filename || 'video.mp4', caption: '🎬' });
            else if (type === 'image') await trackedSend(from, { image: buf, caption: filename || '' });
            fs.unlink(filePath, () => {});
        } catch (e) { console.error(`[${type}]`, e.message); }
    };

    // Support both lists (new engine) and single strings (legacy commands)
    if (data.audio_list && Array.isArray(data.audio_list)) {
        for (let i = 0; i < data.audio_list.length; i++) {
            await sendFile(data.audio_list[i], 'audio', data.filenames?.[i]);
        }
    } else if (data.audio) {
        await sendFile(data.audio, 'audio', data.filename);
    }

    if (data.video_list && Array.isArray(data.video_list)) {
        for (let i = 0; i < data.video_list.length; i++) {
            await sendFile(data.video_list[i], 'video', data.filenames?.[i]);
        }
    } else if (data.video) {
        await sendFile(data.video, 'video', data.filename);
    }

    if (data.image_list && Array.isArray(data.image_list)) {
        for (let i = 0; i < data.image_list.length; i++) {
            await sendFile(data.image_list[i], 'image', data.filenames?.[i]);
        }
    } else if (data.image) {
        await sendFile(data.image, 'image', data.filename);
    } else if (data.image_base64 || data.media_base64) {
        try {
            const b64 = data.image_base64 || data.media_base64;
            const buf = Buffer.from(b64, 'base64');
            await trackedSend(from, { image: buf, caption: data.filename || '' });
        } catch (e) {
            console.error('[Image send b64]', e.message);
        }
    }

    // Send stickers
    if (data.sticker_list && Array.isArray(data.sticker_list)) {
        for (const stk of data.sticker_list) {
            try {
                const buf = fs.existsSync(stk) ? fs.readFileSync(stk) : Buffer.from(stk, 'base64');
                const quotedObj = (data.reply_to_quoted && quotedFake) ? quotedFake : originalMsg;
                await trackedSend(from, { sticker: buf }, { quoted: quotedObj });
            } catch (e) { console.error('[Sticker list send]', e.message); }
        }
    } else if (data.sticker) {
        try {
            const buf = fs.existsSync(data.sticker)
                ? fs.readFileSync(data.sticker)
                : Buffer.from(data.sticker, 'base64');
            const quotedObj = (data.reply_to_quoted && quotedFake) ? quotedFake : originalMsg;
            await trackedSend(from, { sticker: buf }, { quoted: quotedObj });
        } catch (e) { console.error('[Sticker send]', e.message); }

    // Send text reply
    } else if (data.reply) {
        sentText = data.reply;
        console.log(`[DEBUG] Attempting to send text reply: ${data.reply}`);

        // Build mentions list:
        const mentions = [];
        if (data.reply_to_quoted && quotedAuthor) {
            mentions.push(normalizeJid(quotedAuthor) || quotedAuthor);
        }

        // Dynamically parse @phone mentions inside the text response
        const mentionRegex = /@(\d+)/g;
        let match;
        while ((match = mentionRegex.exec(data.reply)) !== null) {
            const jid = `${match[1]}@s.whatsapp.net`;
            if (!mentions.includes(jid)) {
                mentions.push(jid);
            }
        }

        try {
            await trackedSend(from,
                { text: data.reply, mentions: mentions.length ? mentions : undefined },
                { quoted: data.reply_to_quoted && quotedFake ? quotedFake : originalMsg }
            );
            console.log(`[DEBUG] Reply sent successfully!`);
        } catch (e) {
            console.error('[Reply Error]', e.message);
        }
    }

    // POST captured message IDs to Flask for self-correction
    if (sentMessageIds.length > 0) {
        try {
            await axios.post('http://127.0.0.1:5000/sent_ids', {
                jid: from,
                message_ids: sentMessageIds,
                sent_text: sentText
            }, { timeout: 3000 });
        } catch (e) {
            // Best-effort: self-correction disabled if Flask unreachable
            console.debug('[SENT_IDS] POST failed:', e.message);
        }
    }
}

startBot();

// ─── HTTP API for Progress Updates & Health ────────────────────────────────────
const http = require('http');
http.createServer((req, res) => {
    if (req.method === 'POST' && req.url === '/send_message') {
        let body = '';
        req.on('data', chunk => body += chunk.toString());
        req.on('end', async () => {
            try {
                const { jid, text } = JSON.parse(body);
                if (!sock) {
                    res.writeHead(503, { 'Content-Type': 'application/json' });
                    return res.end(JSON.stringify({ ok: false, error: 'bridge_not_connected' }));
                }
                if (!jid) {
                    res.writeHead(400, { 'Content-Type': 'application/json' });
                    return res.end(JSON.stringify({ ok: false, error: 'missing_jid' }));
                }
                const target = normalizeJid(jid) || jid;
                let sent_key = null;
                try {
                    const r = await instrumentedSend(target, { text });
                    sent_key = r && r.key;
                } catch (err) {
                    console.error('[API] sendMessage error:', err.message);
                    res.writeHead(500, { 'Content-Type': 'application/json' });
                    return res.end(JSON.stringify({ ok: false, error: err.message }));
                }
                // Best-effort typing indicator refresh
                sock.sendPresenceUpdate('composing', target).catch(() => {});
                res.writeHead(200, { 'Content-Type': 'application/json' });
                return res.end(JSON.stringify({
                    ok: true,
                    message_id: sent_key?.id || null,
                    message_key: sent_key || null,
                    ts: Date.now(),
                }));
            } catch (e) {
                console.error('[API] /send_message error:', e.message);
                res.writeHead(500, { 'Content-Type': 'application/json' });
                res.end(JSON.stringify({ ok: false, error: e.message }));
            }
        });
        return;
    }

    if (req.method === 'POST' && req.url === '/edit_message') {
        let body = '';
        req.on('data', chunk => body += chunk.toString());
        req.on('end', async () => {
            try {
                const { jid, message_id, new_text } = JSON.parse(body);
                if (!sock) {
                    res.writeHead(503, { 'Content-Type': 'application/json' });
                    return res.end(JSON.stringify({ ok: false, error: 'bridge_not_connected' }));
                }
                if (!jid || !message_id || new_text === undefined) {
                    res.writeHead(400, { 'Content-Type': 'application/json' });
                    return res.end(JSON.stringify({ ok: false, error: 'missing_fields' }));
                }
                const target = normalizeJid(jid) || jid;
                try {
                    await instrumentedSend(target, {
                        text: new_text,
                        edit: { remoteJid: target, id: message_id, fromMe: true },
                    });
                } catch (err) {
                    console.error('[API] editMessage error:', err.message);
                    res.writeHead(500, { 'Content-Type': 'application/json' });
                    return res.end(JSON.stringify({ ok: false, error: err.message }));
                }
                res.writeHead(200, { 'Content-Type': 'application/json' });
                res.end(JSON.stringify({ ok: true, message_id }));
            } catch (e) {
                console.error('[API] /edit_message error:', e.message);
                res.writeHead(500, { 'Content-Type': 'application/json' });
                res.end(JSON.stringify({ ok: false, error: e.message }));
            }
        });
        return;
    }

    if (req.method === 'POST' && req.url === '/delete_message') {
        let body = '';
        req.on('data', chunk => body += chunk.toString());
        req.on('end', async () => {
            try {
                const { jid, message_id } = JSON.parse(body);
                if (!sock) {
                    res.writeHead(503, { 'Content-Type': 'application/json' });
                    return res.end(JSON.stringify({ ok: false, error: 'bridge_not_connected' }));
                }
                if (!jid || !message_id) {
                    res.writeHead(400, { 'Content-Type': 'application/json' });
                    return res.end(JSON.stringify({ ok: false, error: 'missing_fields' }));
                }
                const target = normalizeJid(jid) || jid;
                try {
                    await instrumentedSend(target, {
                        delete: { remoteJid: target, id: message_id, fromMe: true },
                    });
                } catch (err) {
                    console.error('[API] deleteMessage error:', err.message);
                    res.writeHead(500, { 'Content-Type': 'application/json' });
                    return res.end(JSON.stringify({ ok: false, error: err.message }));
                }
                res.writeHead(200, { 'Content-Type': 'application/json' });
                res.end(JSON.stringify({ ok: true, message_id }));
            } catch (e) {
                console.error('[API] /delete_message error:', e.message);
                res.writeHead(500, { 'Content-Type': 'application/json' });
                res.end(JSON.stringify({ ok: false, error: e.message }));
            }
        });
        return;
    }

    if (req.method === 'POST' && req.url === '/post_status') {
        let body = '';
        req.on('data', chunk => body += chunk.toString());
        req.on('end', async () => {
            try {
                const { text, media_base64, mimetype } = JSON.parse(body);
                if (sock) {
                    const statusJids = Array.from(seenContacts);
                    const options = statusJids.length ? { statusJidList: statusJids } : {};
                    if (media_base64) {
                        const buffer = Buffer.from(media_base64, 'base64');
                        if (mimetype && mimetype.startsWith('image/')) {
                            await instrumentedSend('status@broadcast', { image: buffer, caption: text }, options);
                        } else if (mimetype && mimetype.startsWith('video/')) {
                            await instrumentedSend('status@broadcast', { video: buffer, caption: text }, options);
                        }
                    } else {
                        await instrumentedSend('status@broadcast', { text: text }, options);
                    }
                    console.log(`[STATUS] Successfully posted status update: ${text || '[Media]'}`);
                }
                res.writeHead(200);
                res.end('OK');
            } catch (e) {
                console.error('[API] /post_status error:', e.message);
                res.writeHead(500);
                res.end(e.message);
            }
        });
        return;
    }
    
    // Default healthcheck
    if (req.url === '/health/full') {
        const botId  = sock?.user?.id || '';
        const botNum = botId.split(':')[0].split('@')[0] || null;
        res.writeHead(200, { 'Content-Type': 'application/json' });
        return res.end(JSON.stringify({
            status: 'ok',
            connected: sock !== null && sock?.user?.id != null,
            bot_num: botNum,
            send_count: sendCount,
            receive_count: receiveCount,
            last_activity_at: lastActivityAt,
            last_send_at: lastSendAt,
            silence_ms: Date.now() - lastActivityAt,
            last_error: lastErrorMsg,
            last_error_at: lastErrorAt,
            recent_events: recentEvents.slice(-10),
        }));
    }

    res.writeHead(200, { 'Content-Type': 'application/json' });
    res.end(JSON.stringify({ status: 'Crimson-Bridge is ALIVE', time: new Date() }));
}).listen(process.env.PORT || 7860, () => {
    console.log('[UPTIME] 🟢 HTTP Server running on port 7860 (API & Health)');
});
