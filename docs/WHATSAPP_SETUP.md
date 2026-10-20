# WhatsApp alerts: setup guide

There are two ways to get alerts on WhatsApp. Choose one on **Alert settings → WhatsApp → Send through**.

| | CallMeBot | Meta WhatsApp Cloud API |
|---|---|---|
| Alerts arrive on | your own mobile number | your own mobile number |
| Sent from | CallMeBot's number | your business sender number |
| Setup | 2 minutes per person | 30–60 minutes, once |
| Needs | nothing | a Meta Business account and a sender number that is **not** on WhatsApp (or Meta's test number) |
| Reliability | free third-party service, best effort | official, reliable |

> Your personal number can always **receive** alerts. What Meta does not allow is using a number
> that is already registered on WhatsApp as the **sender** of the Cloud API. That is why the
> "Phone number ID" field is not your phone number.

## Option A: CallMeBot (easiest, alerts to your own number)

Each person who wants alerts does this once on their phone:

1. Add **+34 644 51 95 23** to the phone's contacts (name it "CallMeBot").
   The number sometimes changes; check <https://www.callmebot.com/blog/free-api-whatsapp-messages/>.
2. Send it this WhatsApp message: `I allow callmebot to send me messages`
3. Wait for the reply with your **API key** (a number like `123456`).

Then in Spike Net Monitor:

1. **Alert settings** → tick **Send WhatsApp alerts** → Send through: **CallMeBot** → **Save settings**.
2. **Users** → edit the person → WhatsApp number (e.g. `+961 79 040 395`), tick **Send me WhatsApp alerts**,
   paste the **CallMeBot API key** → Save.
3. **Alert settings** → **Send WhatsApp test** with that number.

## Option B: Meta WhatsApp Cloud API (official)

Meta's free tier covers normal alert volumes
(you pay only above Meta's free monthly conversations; check Meta's current pricing).

**Quick test with Meta's free test number:** in step 2 below you can skip adding your own sender:
the API Setup page gives you a free **test number** and its Phone number ID. Under **To**, add and verify
up to 5 recipient numbers (for example your own mobile). This is enough to try everything; for daily use,
add a real sender number.

## What you need

- A Facebook account (for the Meta Business account).
- A **phone number that is not used on WhatsApp** (a new SIM is easiest). It becomes the "sender".
  It must be able to receive an SMS or a call once, for verification.
- The WhatsApp numbers of the people who should receive alerts.

## 1. Create the Meta app

1. Go to <https://developers.facebook.com/> → **My Apps** → **Create app**.
2. Choose **Other** → **Business** type, give it a name (e.g. "Spike Net Alerts"), and create or select
   your **Meta Business account**.
3. In the app dashboard, find **WhatsApp** and click **Set up**.

## 2. Add your sender phone number

1. In **WhatsApp → API Setup**, click **Add phone number**.
2. Enter the display name (e.g. "Spike Net NOC"), your business details, and the phone number.
3. Verify it with the SMS / call code.
4. Copy the **Phone number ID** shown under the number (a long number like `123456789012345`).
   This is *not* the phone number itself.

## 3. Create a permanent access token

The token shown on the API Setup page expires after 24 hours, so make a permanent one:

1. Open **Business Settings** (<https://business.facebook.com/settings>) → **Users → System users** → **Add**.
   Name it "spike-monitor", role **Admin**.
2. Click **Add assets** → **Apps** → select your app → **Full control**.
3. Also under **Accounts → WhatsApp accounts**, add the system user to your WhatsApp account with full control.
4. Back on the system user, click **Generate new token** → select your app →
   expiration **Never** → permissions **whatsapp_business_messaging** and **whatsapp_business_management**.
5. Copy the token now (it is shown only once).

## 4. Create the alert message template

WhatsApp only allows a business to start a conversation with an approved **template**.

1. Open **WhatsApp Manager** (<https://business.facebook.com/wa/manage/message-templates/>) → **Create template**.
2. Category: **Utility**. Name: `spike_alert`. Language: **English** (or Arabic, see below).
3. Body text, exactly with one variable:

   ```
   Spike Net alert: {{1}}
   ```

   Add an example for the variable, e.g. `DOWN: Tower 1 PTP (10.0.1.1)`.
4. Submit. Approval usually takes a few minutes to a few hours.

For Arabic, create the template in Arabic and set **Template language code** to `ar` in the app.

## 5. Enter the settings in Spike Net Monitor

1. Log in as admin → **Alert settings**.
2. Tick **Send WhatsApp alerts**, paste the **Phone number ID** and the **Access token**.
3. Message type: **Template**, template name `spike_alert`, language `en` (or `ar`).
4. **Save settings**, then use **Send WhatsApp test** with your own number.
   The result (sent / the exact error from Meta) appears under **Recent notifications**.

## 6. Choose who gets alerts

**Users** → edit each person:

- **WhatsApp number** in international format, e.g. `+961 70 123 456`.
- Tick **Send me WhatsApp alerts**.
- **Alerts to receive**: critical only / critical + warnings / everything.
- **Only for these sites**: leave empty for all sites, or pick the towers this technician covers.

## Troubleshooting

| Error | Fix |
|---|---|
| `Invalid OAuth access token` | The token expired or was copied wrong; make a permanent system-user token (step 3). |
| `Template name does not exist in the translation` | The template name or language code doesn't match the approved template exactly. |
| `Recipient phone number not in allowed list` | The app is still in development mode with a test number; add the recipient in API Setup, or use your verified real number. |
| Messages arrive only sometimes | In **Plain text** mode, WhatsApp only delivers within 24 h after that person wrote to your number. Use **Template** mode. |
