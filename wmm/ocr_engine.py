"""OCR engine for receipts: PP-OCRv5 Russian/English via ONNX on CPU.

Models are downloaded and verified when building the Docker image, not when
a customer sends a receipt. Tesseract is only the recoverable fallback.
"""
from __future__ import annotations

import logging
import os
from functools import lru_cache
from PIL import Image

LOG = logging.getLogger(__name__)


@lru_cache(maxsize=1)
def get_rapidocr():
    from rapidocr import RapidOCR, EngineType, LangDet, LangRec, ModelType, OCRVersion

    params = {
        "Det.engine_type": EngineType.ONNXRUNTIME,
        "Det.lang_type": LangDet.CH,
        "Det.model_type": ModelType.MOBILE,
        "Det.ocr_version": OCRVersion.PPOCRV5,
        "Rec.engine_type": EngineType.ONNXRUNTIME,
        "Rec.lang_type": LangRec.ESLAV,
        "Rec.model_type": ModelType.MOBILE,
        "Rec.ocr_version": OCRVersion.PPOCRV5,
    }
    return RapidOCR(params=params)


def rapid_text(image: Image.Image) -> str:
    import numpy as np

    result = get_rapidocr()(np.asarray(image.convert("RGB")))
    if not result or not result.txts:
        return ""
    # Reading order is returned by the text detector; retain it for
    # pairing a product line with the separate quantity/price line.
    return "\n".join(
        text.strip()
        for text, confidence in zip(result.txts, result.scores)
        if confidence >= 0.40 and text.strip()
    )


def tesseract_text(image: Image.Image) -> str:
    from PIL import ImageEnhance, ImageOps
    import pytesseract

    gray = ImageEnhance.Contrast(ImageOps.grayscale(image)).enhance(1.5)
    if gray.width < 1400:
        scale = min(2.5, 1400 / gray.width)
        gray = gray.resize((int(gray.width * scale), int(gray.height * scale)))
    if max(gray.size) > 3200:
        gray.thumbnail((3200, 3200))
    return pytesseract.image_to_string(gray, lang="rus", config="--psm 6", timeout=25)


def read_text(image: Image.Image) -> str:
    preferred = os.getenv("WMM_OCR_ENGINE", "rapidocr").lower().strip()
    if preferred not in ("rapidocr", "tesseract"):
        raise ValueError("WMM_OCR_ENGINE must be rapidocr or tesseract")
    if preferred == "tesseract":
        return tesseract_text(image)
    try:
        recognized = rapid_text(image)
        if recognized:
            return recognized
        LOG.warning("PP-OCRv5 found no receipt text; attempting Tesseract fallback")
    except Exception as exc:
        LOG.warning("PP-OCRv5 failed (%s); attempting Tesseract fallback", type(exc).__name__)
    return tesseract_text(image)
