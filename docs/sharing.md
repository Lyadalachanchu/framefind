# Sharing FrameFind

## Links and media

- [Source and setup](https://github.com/Lyadalachanchu/framefind)
- [MP4 demo](media/framefind-demo.mp4)
- [Animated preview](media/framefind-demo.gif)
- [Architecture](architecture.md)

The recording shows the actual app searching [The Most Overlooked Taste in the World](https://www.youtube.com/watch?v=zC1v19wfoTA&t=6s) by **Rabbit Hole** for **“tap”**, playing a result, and exporting a snippet. It starts with the video ready to search and uses saved Jev matches. Pauses are shortened, and the recording is silent. Preserve the footage credit in [NOTICE](../NOTICE.md) alongside any reposted demo.

## Show HN draft

**Title**

Show HN: FrameFind – Find moments in videos with natural-language queries

**URL**

https://github.com/Lyadalachanchu/framefind

**Introductory comment**

FrameFind is a small local web app for finding visual moments in video. Upload a clip or paste a public video URL, type a description, then play or export the matching snippets. The README has a short demo.

It uses Python/FastAPI, FFmpeg, SQLite, and plain JavaScript. FFmpeg prepares every tenth frame locally. Each new query checks the sampled frames with TypeSafe Jev, including neighboring frames for a little motion context. Repeated queries reuse saved matches. A first search takes time and makes paid API calls as Jev checks the frames.

The UI and media library run locally, but sampled images and search text go to TypeSafe; you bring your own API key. Results use a fixed 95% relevance-score cutoff, which is not an accuracy guarantee. It searches visuals, not audio, and brief events can fall between samples.

The code is MIT licensed, with setup instructions, tests that need no API key, and contribution guidelines. Feedback on search quality, the import/playback flow, and the tradeoff between sampling density and per-query work would be useful.

## Use the demo in a README

The repository README already uses the animated preview as a clickable link to the MP4:

```markdown
[![Watch the FrameFind demo](docs/media/framefind-demo.gif)](docs/media/framefind-demo.mp4)
```

For another repository or a blog, upload or link the MP4 and preserve the source-footage credit and terms in [NOTICE](../NOTICE.md).
