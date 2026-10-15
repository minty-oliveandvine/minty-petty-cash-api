"""Best-effort file downsizing to a 1 MB target.

Used before persisting uploaded receipts to S3 so storage and presigned-URL
payloads stay small.

Large images are always encoded as JPEG: PNG's exhaustive ``optimize`` pass is
the dominant CPU/RAM cost and the main worker OOM/timeout source for big
receipt uploads, and JPEG is far smaller and cheaper. Because the output format
can differ from the input, ``downsize_bytes`` returns the resulting mime type
alongside the bytes so callers can fix the stored file extension / metadata.

If the file cannot be brought under the target, the smallest produced variant
is returned anyway — callers should not block on size.
"""

import io
import logging

logger = logging.getLogger("petty-cash-api")

TARGET_BYTES = 1 * 1024 * 1024  # 1 MB

# Memory guardrail: a single uncompressed image must not be allowed to balloon
# RAM beyond this. ~80M pixels decodes to ~320 MB at RGBA. On the 2 GB instance
# across 4 workers that's a safe worst case (~1.3 GB if all four peak at once),
# and it lets us actually downsize large uploads instead of skipping them.
# Roughly Pillow's own MAX_IMAGE_PIXELS (~89M) decompression-bomb threshold.
# Above this we down-scale on decode rather than materializing the full bitmap.
_MAX_DECODE_PIXELS = 80_000_000

_IMAGE_MIMES = {
    "image/jpeg",
    "image/jpg",
    "image/png",
}



def downsize_bytes(data: bytes, mime_type: str) -> tuple[bytes, str]:
    """Return ``(downsized_bytes, output_mime_type)`` for the file.

    The output mime type may differ from the input: large PNGs are re-encoded
    as JPEG. Callers should use the returned mime type to set the stored file
    extension / metadata so the key matches the actual bytes.

    Never raises. On any error, returns the original input and original mime.
    """
    if not data or len(data) <= TARGET_BYTES:
        return data, mime_type

    try:
        if mime_type in _IMAGE_MIMES:
            return _downsize_image(data, mime_type)
        if mime_type == "application/pdf":
            return _downsize_pdf(data), mime_type
    except Exception as exc:
        logger.warning(
            "downsize_bytes: best-effort failed for %s (%s bytes): %s",
            mime_type, len(data), exc,
        )
    return data, mime_type


def _downsize_image(data: bytes, mime_type: str) -> tuple[bytes, str]:
    from PIL import Image

    img = Image.open(io.BytesIO(data))

    # Before fully decoding, cheaply shrink the decode for very large images so
    # we never materialize a huge uncompressed bitmap in RAM. For JPEG, draft()
    # decodes directly at a reduced scale (near-free); for others we cap the
    # size we resize to after load. This is the main OOM guard.
    w0, h0 = img.size
    if w0 * h0 > _MAX_DECODE_PIXELS:
        scale = (_MAX_DECODE_PIXELS / (w0 * h0)) ** 0.5
        target = (max(1, int(w0 * scale)), max(1, int(h0 * scale)))
        try:
            img.draft(None, target)  # JPEG fast-path; no-op for formats w/o draft
        except Exception:
            pass
    img.load()

    # Always encode as JPEG. The previous PNG path used optimize=True, whose
    # exhaustive zlib pass is the dominant CPU/RAM cost and the worker
    # OOM/timeout source for large uploads; JPEG is far smaller and cheaper.
    # JPEG has no alpha channel, so flatten any transparency onto white first.
    has_alpha = img.mode in ("RGBA", "LA") or (
        img.mode == "P" and "transparency" in img.info
    )
    if has_alpha:
        rgba = img.convert("RGBA")
        background = Image.new("RGBA", rgba.size, (255, 255, 255, 255))
        flattened = Image.alpha_composite(background, rgba).convert("RGB")
        rgba.close()
        background.close()
        img.close()
        img = flattened
    elif img.mode != "RGB":
        converted = img.convert("RGB")
        if converted is not img:
            img.close()
        img = converted

    best = data
    best_mime = mime_type
    max_dim = 2400
    quality = 85
    for _ in range(7):
        w, h = img.size
        scale = min(1.0, max_dim / max(w, h))
        resized = (
            img.resize((max(1, int(w * scale)), max(1, int(h * scale))), Image.LANCZOS)
            if scale < 1.0
            else img
        )

        buf = io.BytesIO()
        resized.save(
            buf, format="JPEG", quality=quality, optimize=True, progressive=True
        )
        out = buf.getvalue()
        buf.close()
        if resized is not img:
            resized.close()  # release the resized bitmap before the next pass

        if len(out) < len(best):
            best = out
            best_mime = "image/jpeg"
        if len(out) <= TARGET_BYTES:
            break

        max_dim = max(800, int(max_dim * 0.8))
        quality = max(40, quality - 10)

    img.close()
    return best, best_mime


def _downsize_pdf(data: bytes) -> bytes:
    try:
        import pikepdf
    except ImportError:
        logger.warning("downsize_bytes: pikepdf not installed; PDF passthrough")
        return data

    src = io.BytesIO(data)
    out = io.BytesIO()
    with pikepdf.open(src) as pdf:
        _resample_pdf_images(pdf)
        pdf.save(
            out,
            object_stream_mode=pikepdf.ObjectStreamMode.generate,
            compress_streams=True,
            linearize=False,
        )
    result = out.getvalue()
    return result if len(result) < len(data) else data


def _resample_pdf_images(pdf, target_max_dim: int = 1600, jpeg_quality: int = 65) -> None:
    """In-place: downscale + JPEG-recompress embedded images larger than target.

    Skips masks/transparent images and anything PIL can't decode round-trip.
    Per-image failures are swallowed so one bad XObject doesn't break the file.
    """
    import pikepdf
    from PIL import Image

    # Cap how large a single embedded image we'll fully decode. A multi-page
    # scanned PDF can hold several huge images; decoding them all to RGB bitmaps
    # at once is the dominant OOM source. Skip anything above the cap rather
    # than risk the worker. Pillow's global guard also raises instead of
    # silently eating memory on a decompression bomb.
    prev_max_pixels = Image.MAX_IMAGE_PIXELS
    Image.MAX_IMAGE_PIXELS = _MAX_DECODE_PIXELS

    seen = set()
    try:
        for page in pdf.pages:
            try:
                xobjects = page.Resources.get("/XObject")
            except Exception:
                continue
            if xobjects is None:
                continue
            for name in list(xobjects.keys()):
                pil = None
                resized = None
                try:
                    obj = xobjects[name]
                    key = (obj.objgen if hasattr(obj, "objgen") else id(obj))
                    if key in seen:
                        continue
                    seen.add(key)
                    if obj.get("/Subtype") != pikepdf.Name("/Image"):
                        continue
                    if "/SMask" in obj or "/Mask" in obj:
                        continue

                    # Check declared dimensions BEFORE decoding. An image whose
                    # pixel count exceeds the cap is skipped entirely so we never
                    # build its full bitmap in RAM (the OOM trigger).
                    try:
                        iw = int(obj.get("/Width", 0))
                        ih = int(obj.get("/Height", 0))
                    except Exception:
                        iw = ih = 0
                    if iw and ih and iw * ih > _MAX_DECODE_PIXELS:
                        logger.warning(
                            "PDF image resample skipped oversized XObject %sx%s", iw, ih
                        )
                        continue

                    pdfimg = pikepdf.PdfImage(obj)
                    pil = pdfimg.as_pil_image()
                except Exception:
                    continue

                try:
                    w, h = pil.size
                    if max(w, h) <= 1000:
                        continue
                    scale = target_max_dim / max(w, h)
                    new_size = (max(1, int(w * scale)), max(1, int(h * scale)))
                    resized = pil.resize(new_size, Image.LANCZOS)
                    if resized.mode != "RGB":
                        rgb = resized.convert("RGB")
                        if rgb is not resized:
                            resized.close()
                        resized = rgb

                    buf = io.BytesIO()
                    resized.save(
                        buf, format="JPEG", quality=jpeg_quality, optimize=True, progressive=True,
                    )
                    jpeg_bytes = buf.getvalue()
                    buf.close()

                    obj.write(jpeg_bytes, filter=pikepdf.Name("/DCTDecode"))
                    obj.Width = resized.width
                    obj.Height = resized.height
                    obj.BitsPerComponent = 8
                    obj.ColorSpace = pikepdf.Name("/DeviceRGB")
                    for k in ("/DecodeParms", "/Decode"):
                        if k in obj:
                            del obj[k]
                except Exception as exc:
                    logger.warning("PDF image resample skipped one XObject: %s", exc)
                    continue
                finally:
                    # Release decoded bitmaps now so peak RAM stays bounded to a
                    # single image, not the whole document.
                    if resized is not None and resized is not pil:
                        resized.close()
                    if pil is not None:
                        pil.close()
    finally:
        Image.MAX_IMAGE_PIXELS = prev_max_pixels
