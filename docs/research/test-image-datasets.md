# Test & sample image datasets for a photo manager

Survey (2026-06-16) of public image datasets a developer can use to test a
photo-management/gallery system and compare its behaviour against other tools.
Written to answer "what's already out there?" before investing further in
Fauxcasa's own synthetic test-data generator. Original Fauxcasa research
(`CC-BY-SA-4.0`); see `NOTICE.md`.

## TL;DR — the landscape splits in two, and neither half fits us

"Image test datasets" is really two non-overlapping worlds:

1. **Metadata / interoperability test sets** — small, hand-built files that
   exercise EXIF/IPTC/XMP edge cases and let you check whether you read *and
   write* metadata the same way other programs do. **This is the world that
   matters for a Picasa-style manager.**
2. **ML / computer-vision datasets** — huge bags of real photos with content
   labels (ImageNet, COCO, faces). Relevant to us only as *bulk realistic
   images* and, later, for the face-recognition angle.

**The gap:** there is no standard *personal photo library* benchmark — nothing
canonical that models nested folders + albums + ratings + captions + keywords +
face regions + sidecars (`.picasa.ini`, `.pal`, `contacts.xml`) + mixed
RAW/JPEG/HEIC/video + skewed timestamps + deliberately garbled metadata. That
absence is precisely Fauxcasa's synthetic-generator niche.

## Recommended test stack (agreed short list, 2026-06-16)

| Layer | Use | Source |
|---|---|---|
| **Correctness baseline** | Know the right answer for every metadata field | IPTC Reference Images |
| **Edge cases / fuzzing** | Tricky & malformed real files; diff oracle | exif-samples + metadata-extractor corpus + Exiv2 |
| **Volume & realism** | Thumbnailing/scroll/scale, messy real metadata | a slice of Unsplash Lite or YFCC100M metadata |
| **Library structure, sidecars, privacy** | Folders, albums, faces, `.picasa.ini`, durable state | **our synthetic generator** (nothing off-the-shelf covers this) |

The **interoperability angle** ("how does my program behave relative to
others") is best served by the IPTC interoperability tests plus
Exiv2/metadata-extractor as reference implementations — that's how DAM/editor
vendors compare metadata behaviour.

**Next step (tracked in beads):** evaluate these external sets against the
synthetic generator and wire the adopted ones into the test suite as
read-only, local-fetch sources. Face datasets are deferred until a
face-matching algorithm is chosen.

## 1. Metadata & interoperability test sets (most relevant)

- **IPTC Photo Metadata Reference Images** — closest thing to an industry
  standard. Each JPEG fills *every* field of a given standard version with
  self-describing values (the Creator field literally reads
  `Creator1 (ref2024.1)`), so a reader's mistakes are obvious at a glance.
  Versions 2014 → 2024.1. https://iptc.org/standards/photo-metadata/reference-images/
- **IPTC Interoperability Tests** + the **Get IPTC PMD tool** — checks each
  field for XMP vs. IIM presence and flags `CHANGED`/`MISSING`; built for
  cross-tool round-trip verification.
  https://iptc.org/standards/photo-metadata/interoperability-tests/ ·
  https://getpmd.iptc.org/getiptcpmd.html
- **ianare/exif-samples** — de-facto community corpus of tricky JPG/TIFF/HEIC
  files incl. a dedicated GPS folder. CC BY-SA 4.0. **Archived 2025-04**;
  samples now collected in **exif-py**.
  https://github.com/ianare/exif-samples ·
  https://github.com/ianare/exif-samples/tree/master/jpg/gps ·
  https://github.com/ianare/exif-py
- **metadata-extractor** (Drew Noakes) — the library others validate against;
  ships a large real-world corpus of malformed/odd files — gold for fuzzing.
  https://github.com/drewnoakes/metadata-extractor
- **Exiv2** — reference C++ EXIF/IPTC/XMP read+write; useful as an oracle to
  diff our output against. https://exiv2.org/examples.html
- **PixelPeeper** — real photos with full EXIF *plus* preserved Lightroom edit
  settings — good for "edit history in XMP" cases. https://pixelpeeper.com/photos
- **Metadata Working Group (MWG)** conventions — the spec for face *region*
  metadata (`mwg-rs:Regions`) and EXIF/IPTC/XMP reconciliation; directly
  relevant since Picasa face data and `.picasa.ini` must map onto MWG regions.

## 2. Bulk real photos with rich metadata (volume + realism)

- **Unsplash Dataset** — **Lite** (25k photos, ~1GB, freely downloadable) and
  **Full** (5.4M, request access). Keywords + searches included; good for
  thumbnail/scroll stress tests. Images are not redistributable — fine for
  local testing only. https://github.com/unsplash/datasets · https://unsplash.com/data
- **YFCC100M** — 99M Flickr photos + 0.8M videos, all Creative Commons, with
  title/description/camera/**tags**/**geotags** (~49M geotagged). The practical
  artifact is the ~12.5GB *metadata-only* archive (URLs + fields) on AWS Open
  Data — the best public proxy for messy real-world user metadata at scale.
  https://multimediacommons.wordpress.com/yfcc100m-core-dataset/ ·
  https://arxiv.org/pdf/1503.01817 ·
  https://registry.opendata.aws/multimedia-commons/
- **Wikimedia Commons / Flickr Commons** — CC/public-domain files with real,
  varied embedded metadata when redistributable images are needed.

## 3. Faces — DEFERRED (pending algorithm selection)

Face matching is deferred to a later stage; we need the algorithm(s) first, so
these are recorded for when that work starts, not for the current test stack.

- **Labeled Faces in the Wild (LFW)** — 13,233 images of 5,749 *named*
  individuals; the named-identity structure maps onto Picasa "people albums."
  Catalogued at https://facedetection.com/datasets/
- **WIDER FACE** — 32k images / 393k labeled faces with hard
  pose/occlusion/scale cases; for detection edge cases.
  https://www.tensorflow.org/datasets/catalog/wider_face ·
  https://arxiv.org/pdf/1511.06523

These are detection/recognition benchmarks, not album datasets — they'd test a
face *pipeline*, not the library model.

## 4. Classic "standard test images" (compression/quality, for completeness)

USC-SIPI (Lena, Baboon, …), Kodak True Color, TESTIMAGES, and the CMU CIL
collection are the canonical algorithm-benchmark images — fixed visual anchors,
not library/metadata behaviour.
https://en.wikipedia.org/wiki/Standard_test_image · https://testimages.org/ ·
http://www.cs.cmu.edu/~cil/v-images.html

## Privacy constraint (carries through all of the above)

Per `bd remember` key `privacy-real-picasa-data`: committed fixtures must be
**synthetic**. Real Picasa libraries — and downloaded real-photo datasets
(Unsplash/YFCC/Flickr) — are local-benchmark-only; do not commit their images.
The synthetic generator stays the source of committed library/album/face/sidecar
fixtures.
