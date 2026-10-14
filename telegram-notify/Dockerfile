FROM node:22-alpine

# Off-the-shelf Telegram notifier MCP server (stdio). Uses only the native
# fetch API + the official MCP SDK — no Telegram SDK, no long polling at
# startup: messages are sent with a plain sendMessage call, so it can share
# the bot token with another long-polling client without 409 conflicts.
#
# Tools: send_message, send_document, send_photo, send_video, send_audio,
#        get_updates (on demand only).
#
# Config (injected by the gateway): TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID.
RUN npm install --global --no-fund --no-audit telegram-notifier-mcp@1.3.1

# Pinned, non-interactive; the server speaks MCP over stdin/stdout.
ENV NODE_ENV=production

ENTRYPOINT ["telegram-notifier-mcp"]
