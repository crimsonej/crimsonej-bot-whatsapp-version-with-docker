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
let reconnectCount = 0;
const seenContacts = new Set();

// ── Self-awareness observability ─────────────────────────────────────────────
let lastActivityAt = Date.now();
let lastSendAt = null;
let sendCount = 0;
let receiveCount = 0;
let lastErrorAt = null;
let lastErrorMsg = null;
const recentEvents = [];                 // ring buffer, max 30
const lastBotReplyByJid = new Map();
const deletedMessageCache = new Map();
// (jid:originalId) -> ts — dedupes edits that arrive via BOTH messages.update
// AND messages.upsert (protocolMessage.editedMessage) paths.
const lastEditForwarded = new Map();

function isValidWhatsAppJid(jid) {
    return typeof jid === 'string' && jid.includes('@');
}

// ── Per-JID Sequential Processing Queue ──────────────────────────────────────
class PerJidQueueManager {
    constructor() {
        this.queues = new Map();
        this.processing = new Set();
    }

    enqueue(jid, taskFn) {
        if (!jid) return taskFn();
        if (!this.queues.has(jid)) {
            this.queues.set(jid, []);
        }
        return new Promise((resolve, reject) => {
            this.queues.get(jid).push({ taskFn, resolve, reject });
            this._processNext(jid);
        });
    }

    async _processNext(jid) {
        if (this.processing.has(jid)) return;
        const q = this.queues.get(jid);
        if (!q || q.length === 0) {
            this.queues.delete(jid);
            return;
        }

        this.processing.add(jid);
        const { taskFn, resolve, reject } = q.shift();
        try {
            const result = await taskFn();
            resolve(result);
        } catch (err) {
            reject(err);
        } finally {
            this.processing.delete(jid);
            setImmediate(() => this._processNext(jid));
        }
    }
}
const messageQueue = new PerJidQueueManager();

function clearStaleEditCache() {
    const now = Date.now();
    for (const [key, ts] of Array.from(lastEditForwarded.entries())) {
        if (now - ts > 10000) lastEditForwarded.delete(key);
    }
}

function clearStaleBridgeCaches() {
    clearStaleEditCache();
    clearStaleDeleteCache();
    if (lastBotReplyByJid.size > 500) {
        const keys = Array.from(lastBotReplyByJid.keys());
        for (let i = 0; i < keys.length - 250; i++) {
            lastBotReplyByJid.delete(keys[i]);
        }
    }
    if (seenContacts.size > 1000) {
        seenContacts.clear();
    }
}
setInterval(clearStaleBridgeCaches, 120000);


function recordEvent(kind, summary) {
    lastActivityAt = Date.now();
    recentEvents.push({ ts: lastActivityAt, kind, summary });
    if (recentEvents.length > 30) recentEvents.shift();
}

function markDeletedMessage(jid, messageId) {
    if (!jid || !messageId) return;
    deletedMessageCache.set(jid, { id: messageId, ts: Date.now() });
    lastBotReplyByJid.delete(jid);
}

function clearStaleDeleteCache() {
    const now = Date.now();
    for (const [jid, info] of Array.from(deletedMessageCache.entries())) {
        if (now - info.ts > 30000) deletedMessageCache.delete(jid);
    }
}

async function instrumentedSend(jid, content, options) {
    try {
        if (!sock || !sock.user) {
            throw new Error("bridge_not_connected");
        }
        const targetJid = outboundJid(jid) || jid;
        const res = await sock.sendMessage(targetJid, content, options);
        sendCount++;
        lastSendAt = Date.now();
        const kind = typeof content === 'object' ? Object.keys(content)[0] : 'text';
        recordEvent('send', `${kind} -> ${targetJid.split('@')[0]}`);
        return res;
    } catch (e) {
        lastErrorAt = Date.now();
        lastErrorMsg = e && e.message;
        recordEvent('send_fail', `${e && e.message} -> ${String(jid || '').split('@')[0]}`);
        throw e;
    }
}

// Auto-fallback: disable TLS verification after repeated EPROTO failures (debug only)
let eprotoCount = 0;
const EPROTO_THRESHOLD = 3;
const https = require('https');

// Helper: defensively normalize JIDs and extract user portion when domains
// like @lid appear. Preserves valid WhatsApp JID domains (s.whatsapp.net, lid, g.us, etc.)
function normalizeJid(sender) {
    if (!sender || typeof sender !== 'string') return null;
    try {
        // Strip device id after ':' and keep the main addr
        const base = sender.split(':')[0];
        const parts = base.split('@');
        const user = parts[0] || '';
        const domain = parts[1] || '';
        if (!user) return null;
        
        // Known valid WhatsApp domains that must be preserved as-is
        const validDomains = new Set(['s.whatsapp.net', 'g.us', 'lid', 'broadcast', 'newsletter', 'c.us', 'hosted']);
        const safeDomain = (domain && (validDomains.has(domain) || domain.includes('whatsapp'))) ? domain : 's.whatsapp.net';
        return `${user}@${safeDomain}`;
    } catch (e) {
        return null;
    }
}

function outboundJid(jid) {
    return normalizeJid(jid) || jid || null;
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
        mediaUploadTimeoutMs: 300000,
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

            // Clean up old socket listeners to prevent listener leaks & zombie sockets
            try { sock?.ws?.terminate(); } catch (_) {}
            try { sock?.ev?.removeAllListeners(); } catch (_) {}

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
                reconnectCount++;
                if (reconnectCount > 5) {
                    console.warn('[BRIDGE] Exceeded maximum reconnect attempts (5). Restarting bridge process for clean socket state...');
                    setTimeout(() => process.exit(1), 1000);
                } else {
                    console.log(`[BRIDGE] Reconnecting in 5s (attempt ${reconnectCount}/5)...`);
                    setTimeout(startBot, 5000);
                }
            }
        } else if (connection === 'open') {
            reconnectCount = 0;
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
            const jid = msg.key?.remoteJid || 'global';
            recordEvent('receive', `${mtype} from ${jid.split('@')[0]}`);
            await messageQueue.enqueue(jid, () => handleMessage(msg)).catch(err =>
                console.error('[BRIDGE] Handler error:', err.message)
            );
        }
    });

    // ── Incoming message EDITS / REVOKES ─────────────────────────────────────
    // Baileys can surface edits in different shapes depending on LID/session
    // state, so check both `editedMessage` and the direct message payload.
    // REVOKE events (user deleted their message) also come through here with
    // `messageStubType: 1` and no new text — handle those as deletes too.
    sock.ev.on('messages.update', async (updates) => {
        for (const u of updates || []) {
            console.log(`[UPD] ${JSON.stringify(u).slice(0, 220)}`);
            // Track delivery status of OUR OWN sent messages (video/audio/text):
            // PENDING → SENT → DELIVERED → READ. If a sent video stays PENDING
            // forever, the recipient's device never received it.
            if (u?.key?.fromMe) {
                const statusMap = { 0: 'PENDING', 1: 'SENT', 2: 'DELIVERED', 3: 'READ', 4: 'PLAYED' };
                const st = u?.update?.status ?? u?.update?.message?.status;
                console.log(`[FROM-ME] id=${(u?.key?.id || '').slice(0, 14)} jid=${(u?.key?.remoteJid || '').split('@')[0]} status=${statusMap[st] ?? st} raw=${JSON.stringify(u).slice(0, 160)}`);
                continue;
            }
            const rawMsg = u?.update?.message || u?.update || {};
            const updateKey = u?.update?.key || u?.key || {};
            const jid = updateKey?.remoteJid || u?.key?.remoteJid || '';
            // NOTE: Baileys emits u.key.id = the ORIGINAL (edited/deleted) message id,
            // while u.update.key.id is the protocol-notification wrapper id. Always
            // prefer the outer u.key.id so the bot matches the correct session turn.
            const messageId = u?.key?.id || updateKey?.id || '';
            const participant = updateKey?.participant || u?.key?.participant || jid;
            const stubType = u?.update?.messageStubType;

            // ── REVOKE (deletion) path ──────────────────────────────────────────
            // When a user deletes/revokes their message, Baileys surfaces a stub
            // event with `messageStubType: 1` and no new payload. Forward it to
            // the bot so it can clean up its session + delete its own reply.
            if (stubType === 1) {
                console.log(`[REVOKE] jid=${jid.split('@')[0]} from=${participant.split('@')[0]} id=${messageId}`);
                recordEvent('revoke', `${participant.split('@')[0]} deleted ${messageId.slice(0, 8)}`);
                if (!jid || !messageId) continue;
                markDeletedMessage(jid, messageId);
                try {
                    await axios.post(AI_SERVER, {
                        message: '',
                        deleted: true,
                        message_id: messageId,
                        sender: participant,
                        user_phone: participant.split(':')[0].split('@')[0],
                        group_name: jid.endsWith('@g.us') ? jid : null,
                    }, { timeout: 60000 }).catch(err => {
                        console.error('[REVOKE] forward failed:', err.message);
                    });
                } catch (err) {
                    console.error('[REVOKE] unexpected:', err.message);
                }
                continue;
            }

            // ── EDIT path ───────────────────────────────────────────────────────
            const edit = rawMsg?.editedMessage || rawMsg?.message || null;
            const inner = edit?.message || edit || {};
            const newText = inner.conversation
                          || inner.extendedTextMessage?.text
                          || inner.imageMessage?.caption
                          || inner.videoMessage?.caption
                          || inner.documentMessage?.caption
                          || rawMsg?.conversation
                          || rawMsg?.extendedTextMessage?.text
                          || '';
            if (!jid || !messageId || !newText.trim()) {
                console.log(`[EDIT] skipping - jid:${!!jid} messageId:${!!messageId} newText:${!!newText.trim()} stubType:${stubType}`);
                continue;
            }
            clearStaleEditCache();
            const dedupeKey = `${jid}:${messageId}`;
            const lastEditTs = lastEditForwarded.get(dedupeKey);
            if (lastEditTs && Date.now() - lastEditTs < 10000) {
                console.log(`[EDIT] dedupe skip (already forwarded) id=${messageId}`);
                continue;
            }
            lastEditForwarded.set(dedupeKey, Date.now());
            console.log(`[EDIT] jid=${jid.split('@')[0]} from=${participant.split('@')[0]} id=${messageId} new_text=${JSON.stringify(newText)}`);
            recordEvent('edit', `${participant.split('@')[0]} -> ${newText.slice(0, 40)}`);
            try {
                const payload = {
                    message: newText,
                    edited: true,
                    message_id: messageId,
                    sender: participant,
                    user_phone: participant.split(':')[0].split('@')[0],
                    group_name: jid.endsWith('@g.us') ? jid : null,
                };
                console.log('[EDIT] forwarding payload:', JSON.stringify(payload));
                const res = await axios.post(AI_SERVER, payload, { timeout: 60000 }).catch(err => {
                    console.error('[EDIT] forward failed:', err.message);
                    return null;
                });
                if (res?.data) {
                    await sendAIResponse(null, jid, res, null, participant)
                        .catch(e => console.error('[EDIT] response handling failed:', e.message));
                }
            } catch (err) {
                console.error('[EDIT] unexpected:', err.message);
            }
        }
    });

    // ── Incoming message DELETES / REVOKES ───────────────────────────────────
    // If a user deletes their own message, we make the bot treat it as if the
    // conversation was cancelled and remove the most recent bot reply for that
    // sender when one exists. The bot handles the actual message deletion via
    // bridge_delete to avoid double-delete race conditions.
    sock.ev.on('messages.delete', async (deletes) => {
        for (const d of deletes || []) {
            if (!d?.keys?.length) continue;
            for (const key of d.keys) {
                const jid = key?.remoteJid || '';
                const messageId = key?.id || '';
                const sender = key?.participant || jid || '';
                console.log(`[DELETE] raw delete key:`, JSON.stringify(key, null, 2).slice(0, 500));
                if (!jid || !messageId) continue;
                if (key?.fromMe) continue;
                markDeletedMessage(jid, messageId);
                try {
                    await axios.post(AI_SERVER, {
                        message: '',
                        deleted: true,
                        message_id: messageId,
                        sender: sender,
                        user_phone: sender.split(':')[0].split('@')[0],
                        group_name: jid.endsWith('@g.us') ? jid : null,
                    }, { timeout: 60000 }).catch(err => {
                        console.error('[DELETE] forward failed:', err.message);
                    });
                } catch (err) {
                    console.error('[DELETE] unexpected:', err.message);
                }
            }
        }
    });
}

// ─── Message Handler ─────────────────────────────────────────────────────────
async function handleMessage(msg) {
    console.log('[BRIDGE] handleMessage called:', msg.key?.remoteJid, msg.message ? Object.keys(msg.message) : 'no message');
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

    // Mark message as read (blue ticks) — skip for simulated test messages
    if (!msg._simulated) {
        await sock.readMessages([msg.key]).catch(e => console.error('[BRIDGE] readMessages error:', e.message));
    }

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

    // Media type flags (moved up for early filter)
    const hasImage   = !!m.imageMessage;
    const hasVideo   = !!m.videoMessage;
    const hasAudio   = !!m.audioMessage;
    const hasSticker = !!m.stickerMessage;
    const hasDocument = !!m.documentMessage;

    // ── Early filter: ignore system messages with no real content ─────────────
    // Catch protocol messages, stub messages, and other system noise before
    // they reach command handlers or the AI.
    const msgTypeKeys = Object.keys(m || {});
    const hasRealContent = text.trim() || hasImage || hasVideo || hasAudio || hasSticker || hasDocument;
    if (!hasRealContent) {
        console.log(`[DEBUG] Ignoring system/empty message from ${userPhone}: typeKeys=${msgTypeKeys.join(',')}`);
        return;
    }

    // ── Suppress WhatsApp "X deleted this message" notices ───────────────────
    // When a message gets revoked, WhatsApp posts a notice like
    // "[Crimson] @171292623908980 deleted:\n\n<original text>" into the chat.
    // It arrives as a plain inbound message; feeding it to the LLM makes the
    // bot "answer" a deletion (garbage searches, wrong downloads). Drop it.
    if (/@\S+\s+deleted:?\s*\n/i.test(text) || /^\[?[A-Za-z][\w ]{0,30}\]?\s+@\S+\s+deleted/i.test(text)) {
        console.log(`[DEBUG] ignoring deletion notice from ${userPhone} (${text.slice(0, 40)}…)`);
        return;
    }

    // ── Incoming message EDITS (delivered as `message.editedMessage`) ────────
    // Modern WhatsApp clients deliver edits as a top-level `editedMessage`
    // field on the Message (no protocolMessage wrapper, no messages.update
    // event — Baileys' normalizeMessageContent unwraps it, so the only trace
    // of an edit here is this upsert). Extract the new content and forward it
    // to the bot. The node's key.id IS the original message id.
    try {
        if (m.editedMessage) {
            // Handle FutureProofMessage wrapper: may contain a Message object or a fallback string
            const editedWrapper = m.editedMessage;
            const edited = editedWrapper.message || editedWrapper;
            let editText;
            if (typeof edited === 'string') {
                // Fallback case: edited is the raw text string
                editText = edited;
            } else if (edited && typeof edited === 'object') {
                // Normal case: extract text from message object
                editText = edited.conversation
                          || edited.extendedTextMessage?.text
                          || edited.imageMessage?.caption
                          || edited.videoMessage?.caption
                          || edited.documentMessage?.caption
                          || '';
            } else {
                // Unexpected type: treat as empty
                editText = '';
            }
            const origId = msg.key.id || '';
            if (editText.trim() && origId) {
                clearStaleEditCache();
                const dedupeKey = `${from}:${origId}`;
                const lastEditTs = lastEditForwarded.get(dedupeKey);
                if (lastEditTs && Date.now() - lastEditTs < 10000) {
                    console.log(`[EDIT-DIRECT] dedupe skip id=${origId}`);
                } else {
                    lastEditForwarded.set(dedupeKey, Date.now());
                    const editSender = msg.key.participant || from;
                    // Safe logging for from and editSender
                    const fromUser = from && typeof from === 'string' ? from.split('@')[0] : '(unknown)';
                    const editSenderUser = editSender && typeof editSender === 'string' ? editSender.split('@')[0] : '(unknown)';
                    console.log(`[EDIT-DIRECT] jid=${fromUser} from=${editSenderUser} id=${origId} new_text=${JSON.stringify(editText)}`);
                    try {
                        const res = await axios.post(AI_SERVER, {
                            message: editText,
                            edited: true,
                            message_id: origId,
                            sender: editSender,
                            user_phone: editSender.split(':')[0].split('@')[0],
                            group_name: from.endsWith('@g.us') ? from : null,
                        }, { timeout: 60000 }).catch(err => {
                            console.error('[EDIT-DIRECT] forward failed:', err.message);
                            return null;
                        });
                        // The bot replies with {reply, edit_mode, replace_message_id} —
                        // process it so the final edit lands on WhatsApp.
                        if (res?.data) {
                            await sendAIResponse(null, from, res, null, editSender)
                                .catch(e => console.error('[EDIT-DIRECT] response handling failed:', e.message));
                        }
                    } catch (err) {
                        console.error('[EDIT-DIRECT] unexpected:', err.message);
                    }
                }
            }
            return;
        }
    } catch (err) {
        console.error('[EDIT-DIRECT] top-level error:', err.message);
        return;
    }

    // ── Incoming message EDITS (delivered via protocolMessage upsert) ────────
    // Some WhatsApp flows deliver edits as `protocolMessage.editedMessage`
    // inside a messages.upsert rather than a messages.update. Forward those to
    // the bot too (deduped against the messages.update path).
    try {
        const protoMsg = m.protocolMessage;
        if (protoMsg?.editedMessage) {
            const edited = protoMsg.editedMessage;
            const editText = edited.conversation
                          || edited.extendedTextMessage?.text
                          || edited.imageMessage?.caption
                          || edited.videoMessage?.caption
                          || edited.documentMessage?.caption
                          || '';
            const origKey = protoMsg.key || msg.key || {};
            const origId = origKey.id || '';
            const origJid = origKey.remoteJid || from;
            if (editText.trim() && origId) {
                clearStaleEditCache();
                const dedupeKey = `${origJid}:${origId}`;
                const lastEditTs = lastEditForwarded.get(dedupeKey);
                if (lastEditTs && Date.now() - lastEditTs < 10000) {
                    console.log(`[EDIT-UPSERT] dedupe skip id=${origId}`);
                } else {
                    lastEditForwarded.set(dedupeKey, Date.now());
                    const editSender = msg.key.participant || origJid;
                    // Safe logging for origJid and editSender
                    const origJidUser = origJid && typeof origJid === 'string' ? origJid.split('@')[0] : '(unknown)';
                    const editSenderUser = editSender && typeof editSender === 'string' ? editSender.split('@')[0] : '(unknown)';
                    console.log(`[EDIT-UPSERT] jid=${origJidUser} from=${editSenderUser} id=${origId} new_text=${JSON.stringify(editText)}`);
                    try {
                        const res = await axios.post(AI_SERVER, {
                            message: editText,
                            edited: true,
                            message_id: origId,
                            sender: editSender,
                            user_phone: editSender.split(':')[0].split('@')[0],
                            group_name: from.endsWith('@g.us') ? from : null,
                        }, { timeout: 60000 }).catch(err => {
                            console.error('[EDIT-UPSERT] forward failed:', err.message);
                            return null;
                        });
                        if (res?.data) {
                            await sendAIResponse(null, from, res, null, editSender)
                                .catch(e => console.error('[EDIT-UPSERT] response handling failed:', e.message));
                        }
                    } catch (err) {
                        console.error('[EDIT-UPSERT] unexpected:', err.message);
                    }
                }
            }
            return;
        }
    } catch (err) {
        console.error('[EDIT-UPSERT] top-level error:', err.message);
        return;
    }

    // Protocol messages (revokes, disappearing-message wrappers) arrive here as
    // empty upserts, but they are already handled via `messages.update`. Skip
    // them silently instead of logging noise.
    if (m.protocolMessage) {
        return;
    }

    // Ignore empty/unsupported payloads so the bot does not treat blank messages as media.
    if (!text.trim() && !hasImage && !hasVideo && !hasAudio && !hasSticker && !hasDocument) {
        console.log(`[DEBUG] Ignoring empty/unsupported message from ${userPhone} (no text, image, video, audio, sticker, or document)`);
        return;
    }

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
        const allowed  = [
            'application/pdf',
            'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
            'application/vnd.openxmlformats-officedocument.presentationml.presentation',
            'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
            'application/vnd.ms-excel',
            'application/vnd.ms-powerpoint',
            'text/plain',
            'text/csv',
            'text/markdown',
            'application/json',
        ];
        const knownExts = ['.pdf','.docx','.doc','.pptx','.ppt','.xlsx','.xls','.csv','.txt','.md','.json','.py','.js','.html','.xml'];
        if (allowed.includes(mimetype) || knownExts.some(ext => fileName.toLowerCase().endsWith(ext))) {
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
            // No media attached — check if it's a text prompt for AI sticker generation
            const prompt = text.replace(/^\/sticker\s*/i, '').trim();
            if (prompt) {
                // Forward to AI for sticker generation
                console.log(`[Sticker] Text-only prompt, forwarding to AI: ${prompt}`);
            } else {
                await instrumentedSend(from, { text: 'Please send or reply to an image/video with /sticker, or provide a prompt like "/sticker happy cat"' }, { quoted: msg });
                return;
            }
        } else {
            await sock.sendPresenceUpdate('composing', from).catch(() => {});
        }

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
        // For text-only prompts, fall through to AI handler for sticker generation
        if (!mediaBuf) {
            return;
        }
        return;
    }

    // ── Vision auto-attach ───────────────────────────────────────────────────
    let imageData = null;
    if (msg._simulatedImageB64) {
        imageData = msg._simulatedImageB64;
    } else if (hasImage || hasSticker) {
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

    let typingInterval = null;
    try {
        // Show "typing..." indicator immediately and keep it active
        await sock.sendPresenceUpdate('composing', from).catch(() => {});
        typingInterval = setInterval(() => {
            sock.sendPresenceUpdate('composing', from).catch(() => {});
        }, 10000); // Refresh every 10s

        try {
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
            }, { timeout: 180000 });
            
            console.log('[BRIDGE] AI response status:', res.status, 'keys:', Object.keys(res.data || {}));
            await sendAIResponse(msg, from, res, quotedFake, quotedSender);
        } catch (e) {
            console.error('[BRIDGE] AI error:', e.message, e.code || '', e.response?.status || '');
        } finally {
            if (typingInterval) clearInterval(typingInterval);
        }
    } catch (e) {
        console.error('[BRIDGE] handleMessage outer error:', e.message);
    }
}

// ─── AI Response Dispatcher ──────────────────────────────────────────────────
async function sendAIResponse(originalMsg, from, response, quotedFake, quotedAuthor) {
    if (!response?.data) return;
    const data = response.data;
    const sendJid = outboundJid(from) || from;
    clearStaleDeleteCache();
    const deletedInfo = deletedMessageCache.get(from);
    if (deletedInfo && Date.now() - deletedInfo.ts < 30000) {
        deletedMessageCache.delete(from);
        console.log(`[DELETE] skipped bot response to ${from.split('@')[0]} after deletion event`);
        return;
    }

    // Collect message IDs for self-correction
    const sentMessageIds = [];
    let sentText = '';

    // Helper to wrap instrumentedSend and capture message_id
    const trackedSend = async (jid, content, options) => {
        const targetJid = outboundJid(jid) || jid;
        const res = await instrumentedSend(targetJid, content, options);
        if (res?.key?.id) {
            sentMessageIds.push(res.key.id);
            lastBotReplyByJid.set(targetJid, res.key.id);
        }
        return res;
    };

    // Send file attachments
    const sendFile = async (filePath, type, filename) => {
        if (!filePath || !fs.existsSync(filePath)) return;
        try {
            const buf = fs.readFileSync(filePath);
            if      (type === 'audio') await trackedSend(sendJid, { audio: buf, mimetype: 'audio/ogg; codecs=opus', ptt: data.ptt || false });
            else if (type === 'video') await trackedSend(sendJid, { video: buf, mimetype: 'video/mp4', fileName: filename || 'video.mp4', caption: '🎬' });
            else if (type === 'image') await trackedSend(sendJid, { image: buf, caption: filename || '' });
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
            await trackedSend(sendJid, { image: buf, caption: data.filename || '' });
        } catch (e) {
            console.error('[Image send b64]', e.message);
        }
    }

    // Send generated document files (create_document tool output)
    if (data.file_path && fs.existsSync(data.file_path)) {
        try {
            const buf = fs.readFileSync(data.file_path);
            const fname = data.file_name || 'document';
            const fmt = (data.file_format || '').toLowerCase();
            const mimeMap = {
                pdf:  'application/pdf',
                docx: 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
                pptx: 'application/vnd.openxmlformats-officedocument.presentationml.presentation',
                xlsx: 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
            };
            const mime = mimeMap[fmt] || 'application/octet-stream';
            await trackedSend(sendJid, { document: buf, mimetype: mime, fileName: fname, caption: '' });
            fs.unlink(data.file_path, () => {});
            console.log(`[DocCreate] Sent ${fname} to ${sendJid.split('@')[0]}`);
        } catch (e) {
            console.error('[DocCreate] send error:', e.message);
        }
    } else if (data.document_list && Array.isArray(data.document_list)) {
        for (const docEntry of data.document_list) {
            if (!docEntry.path || !fs.existsSync(docEntry.path)) continue;
            try {
                const buf = fs.readFileSync(docEntry.path);
                const fname = docEntry.filename || 'document';
                const fmt = (docEntry.format || '').toLowerCase();
                const mimeMap = {
                    pdf:  'application/pdf',
                    docx: 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
                    pptx: 'application/vnd.openxmlformats-officedocument.presentationml.presentation',
                    xlsx: 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
                };
                const mime = mimeMap[fmt] || 'application/octet-stream';
                await trackedSend(sendJid, { document: buf, mimetype: mime, fileName: fname, caption: '' });
                fs.unlink(docEntry.path, () => {});
                console.log(`[DocCreate] Sent ${fname} to ${sendJid.split('@')[0]}`);
            } catch (e) {
                console.error('[DocCreate] send error:', e.message);
            }
        }
    }

    // Send stickers
    if (data.sticker_list && Array.isArray(data.sticker_list)) {
        for (const stk of data.sticker_list) {
            try {
                const buf = fs.existsSync(stk) ? fs.readFileSync(stk) : Buffer.from(stk, 'base64');
                const quotedObj = (data.reply_to_quoted && quotedFake) ? quotedFake : originalMsg;
                await trackedSend(sendJid, { sticker: buf }, { quoted: quotedObj });
            } catch (e) { console.error('[Sticker list send]', e.message); }
        }
    } else if (data.sticker) {
        try {
            const buf = fs.existsSync(data.sticker)
                ? fs.readFileSync(data.sticker)
                : Buffer.from(data.sticker, 'base64');
            const quotedObj = (data.reply_to_quoted && quotedFake) ? quotedFake : originalMsg;
            await trackedSend(sendJid, { sticker: buf }, { quoted: quotedObj });
        } catch (e) { console.error('[Sticker send]', e.message); }

    // Send text reply (or in-place edit of a previous bot message)
    } else if (data.reply || (data.edit_mode && data.replace_message_id)) {

        if (data.edit_mode && data.replace_message_id) {
            const finalText = data.reply || '...';
            sentText = finalText;
            try {
                // NOTE: `edit` must live INSIDE the content object, not options —
                // Baileys checks `'edit' in content` to build the protocol message.
                // Passing it in options silently sends a NEW message instead.
                const res = await instrumentedSend(sendJid,
                    { text: finalText, edit: { remoteJid: sendJid, id: data.replace_message_id, fromMe: true } }
                );
                if (res?.key?.id) {
                    lastBotReplyByJid.set(sendJid, res.key.id);
                }
                console.log(`[DEBUG] Edited bot reply in place for ${sendJid.split('@')[0]} -> ${data.replace_message_id}`);
                return;
            } catch (e) {
                console.error('[Reply Error]', e.message);
            }
        }
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
            await trackedSend(sendJid,
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
                jid: sendJid,
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
    req.setTimeout(300000);
    res.setTimeout(300000);
    if (req.method === 'POST' && req.url === '/send_message') {
        let body = '';
        req.on('data', chunk => body += chunk.toString());
        req.on('end', async () => {
            try {
                const { jid, text, path: mediaPath, media_type, filename } = JSON.parse(body);
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
                    if (mediaPath && fs.existsSync(mediaPath)) {
                        const buf = fs.readFileSync(mediaPath);
                        const mtype = (media_type || 'audio').toLowerCase();
                        let content;
                        if (mtype === 'video') content = { video: buf, mimetype: 'video/mp4', fileName: filename || 'video.mp4', caption: '🎬' };
                        else if (mtype === 'image') content = { image: buf, caption: filename || '' };
                        else if (mtype === 'document') content = { document: buf, mimetype: 'application/octet-stream', fileName: filename || 'file.bin', caption: text || '' };
                        else {
                            const fname = (filename || mediaPath || '').toLowerCase();
                            let audioMime = 'audio/ogg; codecs=opus';
                            if (fname.endsWith('.mp3')) audioMime = 'audio/mpeg';
                            else if (fname.endsWith('.m4a')) audioMime = 'audio/mp4';
                            content = { audio: buf, mimetype: audioMime, ptt: false };
                        }
                        // WhatsApp media CDN is intermittently unreachable; retry before giving up.
                        for (let attempt = 1; attempt <= 3; attempt++) {
                            try {
                                const r = await instrumentedSend(target, content);
                                sent_key = r && r.key;
                                break;
                            } catch (err) {
                                console.error(`[API] sendMessage media attempt ${attempt}/3 failed:`, err.message);
                                if (attempt < 3) {
                                    await new Promise(res => setTimeout(res, 2000 * attempt));
                                } else {
                                    throw err;
                                }
                            }
                        }
                        fs.unlink(mediaPath, () => {});
                    } else {
                        const r = await instrumentedSend(target, { text });
                        sent_key = r && r.key;
                    }
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

    // ── TEST HOOK: /simulate — inject a fabricated user message through the
    // real event handlers (messages.upsert / messages.update) exactly like
    // WhatsApp traffic. Replies go through the normal send path.
    //   {jid, type: 'message'|'edit'|'delete', text, id?, edit_id?, image_base64?, push_name?}
    if (req.method === 'POST' && req.url === '/simulate') {
        let body = '';
        req.on('data', chunk => body += chunk.toString());
        req.on('end', async () => {
            try {
                const p = JSON.parse(body);
                if (!sock) {
                    res.writeHead(503, { 'Content-Type': 'application/json' });
                    return res.end(JSON.stringify({ ok: false, error: 'bridge_not_connected' }));
                }
                const jid = p.jid || '250203957407887@lid';
                const msgId = p.id || (Date.now().toString(36).toUpperCase() + Math.random().toString(36).slice(2, 6).toUpperCase());
                const pushName = p.push_name || 'crimson';
                const baseKey = { remoteJid: jid, fromMe: false, id: msgId, participant: '', addressingMode: 'lid' };
                if (p.type === 'edit') {
                    const fakeMsg = {
                        key: baseKey,
                        message: { editedMessage: { message: { conversation: p.text || '' } } },
                        pushName,
                        messageTimestamp: Math.floor(Date.now() / 1000),
                        _simulated: true,
                    };
                    sock.ev.emit('messages.upsert', { messages: [fakeMsg], type: 'notify' });
                    console.log(`[SIMULATE] edit injected id=${msgId} text=${JSON.stringify(p.text)}`);
                } else if (p.type === 'delete') {
                    const delKey = { ...baseKey, id: p.edit_id || msgId };
                    sock.ev.emit('messages.update', [
                        { key: delKey, update: { message: null, messageStubType: 1, key: delKey } }
                    ]);
                    console.log(`[SIMULATE] delete injected id=${delKey.id}`);
                } else {
                    const fakeMsg = {
                        key: baseKey,
                        pushName,
                        messageTimestamp: Math.floor(Date.now() / 1000),
                        _simulated: true,
                        _simulatedImageB64: p.image_base64 || null,
                    };
                    if (p.image_base64) {
                        fakeMsg.message = { imageMessage: { mimetype: 'image/jpeg', caption: p.text || '' } };
                    } else {
                        fakeMsg.message = { conversation: p.text || '' };
                    }
                    sock.ev.emit('messages.upsert', { messages: [fakeMsg], type: 'notify' });
                    console.log(`[SIMULATE] message injected id=${msgId} text=${JSON.stringify(p.text)} image=${!!p.image_base64}`);
                }
                res.writeHead(200, { 'Content-Type': 'application/json' });
                res.end(JSON.stringify({ ok: true, injected_id: p.type === 'delete' ? (p.edit_id || msgId) : msgId }));
            } catch (e) {
                console.error('[API] /simulate error:', e.message);
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
                // Use the raw jid as-is: the message was sent to this exact jid
                // (e.g. @lid), and edit keys must match the original send key.
                // normalizeJid() would remap @lid → @s.whatsapp.net and the edit
                // would silently fail.
                const target = jid;
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
                // Use the raw jid as-is: the message was sent to this exact jid
                // (e.g. @lid), and delete keys must match the original send key.
                // normalizeJid() would remap @lid → @s.whatsapp.net and the
                // delete would silently fail.
                const target = jid;
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

    if (req.method === 'POST' && req.url === '/group_admins') {
        let body = '';
        req.on('data', chunk => body += chunk.toString());
        req.on('end', async () => {
            try {
                const { jid } = JSON.parse(body);
                if (!sock) {
                    res.writeHead(503, { 'Content-Type': 'application/json' });
                    return res.end(JSON.stringify({ ok: false, error: 'bridge_not_connected' }));
                }
                if (!jid || !jid.endsWith('@g.us') || !isValidWhatsAppJid(jid)) {
                    res.writeHead(400, { 'Content-Type': 'application/json' });
                    return res.end(JSON.stringify({ ok: false, error: 'invalid_group_jid' }));
                }
                const meta = await sock.groupMetadata(jid);
                const admins = (meta.participants || [])
                    .filter(p => p.admin)
                    .map(p => p.id || p.jid)
                    .filter(Boolean);
                res.writeHead(200, { 'Content-Type': 'application/json' });
                return res.end(JSON.stringify({ ok: true, admins }));
            } catch (e) {
                console.error('[API] /group_admins error:', e.message);
                res.writeHead(500, { 'Content-Type': 'application/json' });
                return res.end(JSON.stringify({ ok: false, error: e.message }));
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
