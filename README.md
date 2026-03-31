<img src="https://user-images.githubusercontent.com/46956787/123745285-4d02dc00-d86d-11eb-97f2-c72efcc6e250.png">

[HDoujin Downloader](https://doujindownloader.com/) is a manga and dōjinshi download manager supporting 800+ websites across many different languages.

<p align="center">
  <img src="https://doujindownloader.com/wp-content/uploads/2014/08/hdd_screenshot_1_16_9_32_r144_001.png" width="250" />
  <img src="https://doujindownloader.com/wp-content/uploads/2014/08/hdd_screenshot_1_16_9_32_r144_002.png" width="250" /> 
  <img src="https://doujindownloader.com/wp-content/uploads/2014/08/hdd_screenshot_1_16_9_32_r144_003.png" width="250" />
</p>

## Features

* Runs as a portable executable, with no installation required
* Download, save, and organize your collection based on the manga's metadata
* Track chapters and get notified when new chapters are uploaded
* Convert downloaded manga to ZIP, CBZ, RAR, CBR, 7Z, EPUB, and PDF
* Convert and resize downloaded images
* Clipboard Monitor feature to automatically download copied URLs
* Multiple UI languages (Arabic, Chinese, English, French, German, Italian, Japanese, Russian, Spanish, and more)

## Download

To get started, download the [latest release](https://doujindownloader.com/hdoujin/download.php?host=github) and follow [these easy steps](https://github.com/HDoujinDownloader/HDoujin-Downloader/wiki/Getting-Started).

If you need any help, don't hesitate to [reach out](https://doujindownloader.com/contact/)!

## Standalone IMHentai downloader script

If you want to directly use the IMHentai scraping logic without launching the HDoujin Downloader app, use:

```bash
python3 scripts/imhentai_downloader.py "https://imhentai.xxx/gallery/<id>/"
```

This writes a gallery folder under `downloads/` with:

* `metadata.json` (title, tags, language, page count, upload date, summary, etc.)
* `images/` with all downloaded gallery pages

Useful options:

```bash
python3 scripts/imhentai_downloader.py "<gallery-url>" --metadata-only
python3 scripts/imhentai_downloader.py "<gallery-url>" -o /path/to/output --delay 0.5
```

Tag/listing crawl + filtering + archiving example:

```bash
python3 scripts/imhentai_downloader.py "https://imhentai.xxx/tag/agata/" \
  --required-language english \
  --required-tag agata \
  --max-pages 25 \
  --zip-after-download
```

This crawls gallery URLs from the listing pages, downloads only galleries that match all required filters, and creates a `.zip` archive for each downloaded gallery folder.
