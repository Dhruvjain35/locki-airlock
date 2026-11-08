# README assets

| File | What it is |
|---|---|
| `locki-airlock-banner.svg` | The README banner: the wordmark, the mark (two bars and a chevron in a square) and a detection-box motif. 1280×320, under 8 KB, no external fonts or images. It has its own dark background, so it reads the same in GitHub's light and dark themes. Edit the text in the SVG directly. |

## Product screenshots (planned, none captured yet)

The README has no product screenshots yet. Areeb is redesigning the dashboard on `feature/nvidia-dashboard-ui`, which is not merged. Capture these shots once the final UI is on `main`, then add them to the root README. Until then, don't add image links or placeholders.

| # | File | What it must show |
|---|---|---|
| 1 | `airlock-01-protection-enabled.png` | **Protection enabled.** The Protection view right after **Start protection**: status *Protected*, the session countdown, Jetson presence *Fresh this session*, and *Screen lock: dry run*. |
| 2 | `airlock-02-physical-event.png` | **Physical event detected.** A person in the desk zone: the Mac's stage is *Watchful*, the activity feed shows the spoken presence warning, and no incident is open. |
| 3 | `airlock-03-incident-raised.png` | **Incident raised.** The high-severity correlated incident (unknown USB device within 10 seconds of a person), with its two linked events and the spoken warnings. |
| 4 | `airlock-04-nemotron-investigation.png` | **Nemotron investigation.** The Investigation view after **Explain incident**: the official severity, Nemotron's own risk and confidence, the grounded findings with their evidence references, the tools used and the inference time, marked *LIVE evidence*. |
| 5 | `airlock-05-stop-and-history.png` | **Stop and history.** The *Protection stopped* confirmation (disarmed, investigator access revoked) next to the History view that keeps the incident. |

The labels above are today's. If the redesign renames them, capture the equivalent state. Use WebP (`.webp`) instead of PNG if the file is clearly smaller at the same sharpness. Keep the numbered names so the order is obvious.

### Capture steps on the real dashboard

Use the real system, not fixtures. If a shot can only be made in rehearsal, label it **Rehearsal (synthetic)** in its caption and alt text.

1. On the protected Mac, start with the launcher (**LOCKI AIRLOCK** on the Desktop) and wait for **READY**. Follow [the judge-day runbook](../../scripts/airlock_launcher/RUNBOOK.md).
2. In the owner browser window, set the zoom to 100% and the window to about 1440×900. Close other tabs. Turn on Do Not Disturb so no notifications appear.
3. Press **Start protection**, review the baseline and start. Capture shot 1.
4. A consenting teammate sits in the Jetson's desk zone. Capture shot 2 once the presence warning appears in the feed.
5. Within 10 seconds, they plug in a harmless USB stick that the team provides. Capture shot 3 when the incident appears.
6. Press **Explain incident**, approve exactly that incident's evidence, and wait for the spoken answer. Capture shot 4 when the investigation record has loaded.
7. Press **Stop protection** and confirm. Capture shot 5, then open **History** for the second half of it.
8. Press **Stop protection** before leaving the desk if any step was interrupted, and confirm the investigator's access is revoked.

### Sanitization rules (check every image before committing)

- **No secrets.** No tokens, keys, `~/.locki` contents, terminal windows, or the browser address bar. The owner dashboard address names a browser slot, so crop the address bar out entirely.
- **No identities.** Blur or crop SpacetimeDB identities (64-character hex strings), including the investigator's.
- **No full IDs.** Never show a full incident, session, event or investigation ID. Show at most the first 8 characters, or blur them.
- **No bystanders.** The dashboard shows no camera frames; keep it that way. Don't add photos or camera previews of people. The teammate in shots 2 and 3 consents and is not identifiable.
- **No private network details.** No IP addresses, Tailscale or host names, user names, or paths that contain a user name (for example `/Users/<name>`).
- **No personal device names.** Blur USB device labels or app names that identify a person, such as a phone named after its owner.
- **Strip metadata.** Re-export or run a metadata stripper (for example `exiftool -all= <file>`) before committing.
- **No real evidence beyond these shots.** CONTRIBUTING.md forbids committing camera frames, audio or incident evidence from a real run. These screenshots show only the dashboard, sanitized as above.

### Size and format

- Use web-optimized PNG or WebP at 1600 px wide at most. That is about twice the width of GitHub's README column. Crop to the panel that matters instead of shrinking a full screen.
- Keep each file under about 500 KB. Compress with a lossless optimizer (for example `oxipng`) or with `cwebp` at quality 90.
- Don't commit large GIFs. If motion is needed, link a short MP4 hosted outside the repository, or attach it to a release.
- Write alt text that says what the shot proves, for example "Protection view: protected, Jetson presence fresh, screen lock dry run".

### Adding them to the README

Replace the README's **Screenshots** note with the images in this order. Use `<img src="docs/assets/<file>" alt="..." width="100%">`, or two shots side by side at `width="49%"`, so the page also works on a phone.
