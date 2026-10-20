# Security

FrameFind is a local, single-user application. Run it on `127.0.0.1`; it has no user authentication and is not designed for a public server or a shared network deployment.

## Report a vulnerability

Use **Security → Report a vulnerability** on the repository when private reporting is available. If it is unavailable, open an issue asking the maintainer for a private reporting channel without including exploit details or sensitive data. Do not include API keys, personal videos, sampled frames, or a copy of your database in a public report.

Include the affected version or commit, steps to reproduce, expected impact, and a minimal example using non-sensitive test data. Security fixes target the current default branch; older snapshots do not receive separate maintenance.

## Data and credentials

- The API key is entered through a hidden terminal prompt or read from `TYPESAFE_API_KEY`. The app does not send it to the browser or write it to its database.
- Video preparation runs locally. During search, sampled images and the query are sent to TypeSafe. This is not an offline image-understanding tool.
- Imported videos, extracted images, search caches, and usage records stay in the configured data directory. Protect that directory like the source videos.
- The local spending guard only tracks this app's data directory. It cannot limit other uses of the same provider key.

Keep Python dependencies, FFmpeg, and yt-dlp updated. If a key is exposed, revoke it with the provider; deleting a file or Git commit alone does not invalidate the credential.
