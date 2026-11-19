# Changelog

All notable changes to Usher, newest first. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow the app's own numbering (minor for a feature, patch for a fix). Each release's full notes are on the Releases page.

## [1.4.1]

### Fixed
- In Setup, **Test** now reads only a small part of an app's reply and gives up when the reply takes too long. An address that answers with something huge or too slowly now shows a clear message instead of hanging.

## [1.4.0]

### Added
- After the first account is made, the Setup page now has a step to connect your apps: Jellyfin, Sonarr, Radarr, Prowlarr, Transmission and, if you like, TMDb. Usher suggests addresses where it finds them on their usual ports, each app says where to find its key, and **Test** reads the app's version and shows whether it is supported, works but hasn't been tested yet, or is too old. Only apps that connected are saved, and a saved key is never shown again. You can skip the step; Usher uses what you save after it next restarts.

## [1.3.0]

### Added
- A new Usher with no accounts now prints a one-time setup code in its log and shows a Setup page. Entering the code lets the first person create their account, who becomes the admin. Settings and secrets an admin saves are kept by Usher itself, in `usher.toml` and a file beside it that only Usher can read, and are never shown back in full; the screens for entering them are still to come. Servers that already have accounts keep working as before, with nothing to do.

## [1.2.0]

### Added
- Usher is now published as a ready-made container image for 64-bit Intel/AMD and Arm computers, with one Compose file to run it, so it can be started without building it. The browser setup is still to come: for now it needs a settings file in its data volume (steps in the deploy guide). Servers that build Usher from source keep working as before.

## [1.1.6]

### Changed
- The server image now runs on Python 3.15; update the server to receive it.

## [1.1.4]

### Security
- The server image is rebuilt with the fixed zlib package; update the server to receive it.

## [1.1.0]

### Changed
- Setting up and updating a server no longer needs an SSH key: Usher is fetched over HTTPS from the public repository. An existing install must first point its source at the HTTPS address, then reinstall the update script and delete the unused key (steps in the deploy guide).

## [1.0.1]

### Fixed
- Installs that build Usher from source can update again: the build no longer stops on a comment placed after the base image.

## [1.0.0]

### Added
- Usher is a Home Screen web app for your media server, built for Safari on iPhone, iPad and Mac and written in plain language: search for a movie or show, tap **Get it**, and follow it from Asked to Ready to watch without opening the apps behind it.
- Search shows results as each source answers, with badges for what is already in the library, requested, or on a streaming service. Discover adds picks based on what each person watched or liked, plus trending and popular rows, and a dislike leaves your Discover at once.
- Get it adds a movie with one tap, or lets you choose seasons for a show. Downloads shows everything you asked for with live progress, and a badge for what is new since you last opened the tab. You can retry, stop or remove a request from the app.
- Home works like Jellyfin's: continue watching, next up and the latest, with an app icon badge for what is ready.
- Stalled downloads are replaced by a healthier copy, and Usher says so. Tidy up lists watched movies and seasons you can remove to free space, and Keep takes a title off the list.
- Status shows at a glance whether everything is working, with Health and Storage side by side on iPad and Mac.
- Settings holds your account, this device's notifications and Open titles in, every signed-in device, and About, with the Updates row and this Changelog.
- Notifications arrive by Web Push, set separately for each person.
- Titles with a family rating can be filed in their own folders automatically.
- Every screen opens at once from its last copy and refreshes in the background. Signing out deletes what the device kept.
