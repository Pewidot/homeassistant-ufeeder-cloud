# U Feeder Cloud

[![HACS Custom](https://img.shields.io/badge/HACS-Custom-41BDF5)](https://www.hacs.xyz/docs/faq/custom_repositories/)
[![Add to HACS](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=Pewidot&repository=homeassistant-ufeeder-cloud&category=integration)

Unofficial Home Assistant integration for UBTECH U Feeder. Requires the U Feeder cloud and Home Assistant 2026.10.1 or newer.

- Feed individual compartments, stop feeding and skip preheating.
- Manage weekly feeding schedules and view upcoming meals in a calendar.
- Control cooling, preheating, temperatures and pet detection.
- View live status, temperatures, battery and feeding history.
- Check for firmware updates and view their status. Install updates through the U-Feeder app.

## Use a separate account

Using the same login in Home Assistant and the mobile app can sign the app out.

1. Create a second U Feeder account in the **same country/region** as your main account.
2. In the app, use **Share device** on your main account to invite the second account for each feeder.
3. Sign in to the second account in the app and accept the invitations.
4. Use the second account in Home Assistant. Keep using your main account in the app.

Pairing and device sharing are done in the U Feeder app.

## Install

1. With [HACS](https://www.hacs.xyz/docs/use/) installed, click **Add to HACS** above and download **U Feeder Cloud**.
2. Restart Home Assistant.
3. Open **Settings → Devices & services → Add integration → U Feeder**.
4. Enter the second account's email, password and registration country.

For manual installation, copy `custom_components/ufeeder` into your Home Assistant `custom_components` directory, then follow steps 2–4.

## Plan feedings

### Weekly schedules

Schedules are managed in the integration's configuration dialog:

1. Open **Settings → Devices & services → U Feeder**.
2. Select **Configure** (gear icon) for your account, then choose the feeder.
3. Select **Add feeding schedule**.
4. Choose the **Time**, **Weekdays**, **Compartment** and **Feeding duration**. Leave **Enabled** on and submit.
5. On the feeder's device page, turn on **Automatic feeding**.

Example: select Monday–Friday, 08:00, compartment 1 and 10 minutes for a weekday breakfast.

To change or disable a plan, reopen **Configure → feeder → Edit / enable schedule**. To remove one, choose **Delete schedule** and confirm. Changes are saved to the feeder. It must be online when saving.

- Maximum **10 schedules**, with at least **60 minutes between enabled start times**.
- Times are entered in the Home Assistant timezone. The feeder stores fixed UTC times, so review schedules after daylight-saving changes.
- Scheduled preheating follows the feeder's **Preheat scheduled feeding** and **Preheating target (°C)** settings.
- The **Feeding schedule** calendar shows active plans; use **Configure** to edit them.

### A feeding on a specific date

The feeder's built-in schedules repeat weekly. For a single date, use a Home Assistant automation:

1. Under **Settings → Devices & services → Helpers**, create a **Date and/or time** helper with both date and time, then set the intended feeding time.
2. Create an automation with a **Time** trigger using that helper.
3. Add the action **U Feeder: Feed a compartment** (`ufeeder.feed`). Select the feeder, compartment, duration and optional preheating.

Home Assistant and the feeder must be online at that time. This does not create a weekly plan on the feeder.

Physical feeding has been tested on a real feeder. Firmware installation is not supported in Home Assistant.
