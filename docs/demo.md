# Demo assets

- [29-second MP4](media/framefind-demo.mp4): 1280 × 800, silent, suitable for a post or a README link.
- [Animated preview](media/framefind-demo.gif): smaller preview that GitHub renders directly in a README.
- [Screenshot](media/screenshot.png): the actual results screen.

The recording shows the running application with real Jev responses. It imports the [52-second Sintel trailer](https://media.w3.org/2010/05/sintel/trailer.mp4), searches for **a person**, opens a matching snippet, enables looping, and downloads an MP4. No responses or scores were mocked. The demo uses a separate sample library, not personal videos.

Download/preparation and the search are sped up, as labeled in the video; pauses between actions are removed. Playback excerpts run at normal speed. This is a workflow demonstration, not a latency or accuracy benchmark. Model responses and performance can vary.

## Reproduce the walkthrough

1. Start FrameFind with your own TypeSafe API key.
2. Choose **Import URL** and paste the trailer URL above.
3. Wait for **Ready to search**. Preparation runs locally without API calls.
4. Search for **a person**. This makes paid requests for the sampled frames unless that query is already cached.
5. Open a result, try **Loop snippet**, and choose **Download clip**.

The source contains 126 sampled frames with the current every-tenth-frame sampler. The recorded run found two snippets above the fixed 95% cutoff. Repeating a compatible query uses cached scores.

## Attribution when sharing

Keep the on-screen credit and include: **Sintel trailer, © Blender Foundation | [sintel.org](https://www.sintel.org/), [CC BY 3.0](https://creativecommons.org/licenses/by/3.0/). Visual excerpts shown inside FrameFind; recording edited and captioned.** See [NOTICE.md](../NOTICE.md).

The repository's MIT license does not replace the footage's Creative Commons license.
