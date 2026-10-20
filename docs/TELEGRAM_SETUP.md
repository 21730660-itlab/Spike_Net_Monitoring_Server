# Telegram alerts: setup guide (recommended)

Telegram bots are free, official and have no limit on how many people use them, so this is the
most reliable way to get alerts on your phone. About 5 minutes.

## 1. Create your bot (once)

1. Install Telegram on your phone and open a chat with **@BotFather** (the official one, with a blue tick).
2. Send `/newbot`.
3. Choose a name, e.g. `Spike Net Alerts`, and a username that ends in `bot`, e.g. `spikenet_alerts_bot`.
4. BotFather replies with a **token** like `123456789:AAHk...`. Keep it private.

## 2. Enter it in Spike Net Monitor

1. Log in as admin → **Alert settings** → **Telegram**.
2. Tick **Send Telegram alerts**, paste the token into **Bot token**, click **Save settings**.

## 3. Connect the people (and/or a team group)

- **Each person**: in Telegram, search for your bot's username, open it and press **Start**.
- **A team group** (optional): add the bot to your NOC group and send any message in the group.

Then on **Alert settings**, click **Find chats**. You see each chat's ID:

- Personal chat IDs are positive numbers, e.g. `555123456`. Put each one on that person:
  **Users** → edit → tick **Send me Telegram alerts** → **Telegram chat ID** → Save.
  Their **Alerts to receive** and **Only for these sites** settings apply.
- Group IDs start with `-`, e.g. `-1001234567890`. Put it in **Team group chat ID** and choose what the
  group gets.

## 4. Test

**Alert settings** → **Send Telegram test** with a chat ID. The result appears under **Recent notifications**.

| Error | Fix |
|---|---|
| `401 Unauthorized (check the bot token)` | Token copied wrong; get it again from @BotFather (`/token`). |
| `403 ... press Start first` | The person never opened the bot, or blocked it. Open the bot and press Start. |
| `400 chat not found` | Wrong chat ID; use **Find chats**. |
| Find chats shows nothing | Send a message to the bot (or in the group) first, then click Find chats again. |
