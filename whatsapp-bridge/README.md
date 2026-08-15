# WhatsApp Bridge 🔗

The communication interface for the Groq Bot. It uses `whatsapp-web.js` to create a headless browser instance and stay connected to your WhatsApp.

## 🚀 Getting Started

### Quick Install (Cross-Platform)
Run the automated Python installer to configure the environment and background processes:
```bash
python3 install.py
```
*Note: The installer requires Node.js and PM2 (optional, for background processes).*

### Manual Install
1.  **Install Dependencies:**
    ```bash
    npm install
    ```
2.  **Start the Bridge:**
    ```bash
    node bridge.js
    ```
3.  **Authentication:**
    A QR code will appear in your terminal. Scan it using **WhatsApp > Linked Devices > Link a Device**.

## ⚙️ How it Works

- Listens for incoming messages.
- Forwards messages to the AI Server (`groq-bot`) at `http://localhost:5000/reply`.
- Sends the AI's response back to the user as text, media, or stickers.

## 🛠️ Troubleshooting

- **Puppeteer/Chrome issues?** Ensure Chromium is installed. The bridge looks for it in `/home/joa/chromium-for-bot/chrome`.
- **Connection dropped?** Just restart `node bridge.js`. Your session is saved in `.wwebjs_auth/`.
