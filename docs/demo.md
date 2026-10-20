# Demo assets

- [Short MP4 demo](media/framefind-demo.mp4): silent, suitable for a post or a README link.
- [Animated preview](media/framefind-demo.gif): smaller preview that GitHub renders directly in a README.
- [Screenshot](media/screenshot.png): the actual results screen.

The recording shows FrameFind searching [The Most Overlooked Taste in the World](https://www.youtube.com/watch?v=zC1v19wfoTA&t=6s) by **Rabbit Hole** for **tap**, opening a matching snippet, and downloading an MP4 clip. It starts with the video already prepared and uses saved results from real Jev responses. Pauses between actions are shortened; playback excerpts run at normal speed.

## Reproduce the walkthrough

1. Start FrameFind with your own TypeSafe API key.
2. Choose **Import URL** and paste the YouTube URL above.
3. Wait for **Ready to search** while FrameFind prepares the sampled frames locally.
4. Search for **tap**. Jev checks the sampled frames, with paid requests for any matches that are not already cached.
5. Open a result, try **Loop snippet**, and choose **Download clip**.

The video contains 5,739 sampled frames. The recorded query returns seven matching moments above the fixed 95% relevance cutoff. Repeating a compatible query uses cached scores, as shown in the recording. Search time and results depend on the source video, cache coverage, and provider responses.

## Footage credit

The source footage is [The Most Overlooked Taste in the World](https://www.youtube.com/watch?v=zC1v19wfoTA&t=6s) by **Rabbit Hole**. See [NOTICE.md](../NOTICE.md) for the credit and source-footage terms. Preserve the credit when sharing the demo.
