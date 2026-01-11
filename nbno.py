# -*- coding: utf-8 -*-
import os
import re
import io
import argparse
from math import ceil
from PIL import Image, ImageEnhance
import requests
from pypdf import PdfWriter, PdfReader

###############################################################################
# Utilities
###############################################################################

def sanitize_filename(name):
    return re.sub(r'[^\w\s\-\.æøåÆØÅ]', '', name).strip()

def image_to_pdf_bytes(image):
    buf = io.BytesIO()
    if image.mode != "RGB":
        image = image.convert("RGB")
    image.save(buf, format="PDF", resolution=100.0)
    buf.seek(0)
    return buf.getvalue()

def is_primarily_grayscale(image, color_threshold=0.05, tolerance=30):
    """
    Detect mostly grayscale pages, ignoring slight yellow/sepia tint.
    """
    if image.mode != "RGB":
        image = image.convert("RGB")
    pixels = image.getdata()
    max_samples = 5000
    step = max(1, len(pixels)//max_samples)
    non_gray = 0
    total_samples = 0
    for i, px in enumerate(pixels):
        if i % step != 0:
            continue
        r, g, b = px
        # Allow yellow/brown tint as grayscale
        if max(abs(r-g), abs(r-b), abs(g-b)) > tolerance:
            if max(r,g,b) - min(r,g,b) > 50:
                non_gray += 1
        total_samples += 1
    return (non_gray / total_samples) < color_threshold

def auto_brightness_factor(image, target_white=245, sample_pixels=5000):
    """
    Estimate brightness factor to make background near target_white.
    """
    if image.mode != "RGB":
        image = image.convert("RGB")
    pixels = image.getdata()
    max_samples = min(sample_pixels, len(pixels))
    step = len(pixels) // max_samples
    brightest = 0
    for i, px in enumerate(pixels):
        if i % step != 0:
            continue
        r, g, b = px
        lum = 0.299*r + 0.587*g + 0.114*b
        if lum > brightest:
            brightest = lum
    if brightest == 0:
        return 1.0
    factor = target_white / brightest
    return min(factor, 2.0)

def enhance_grayscale_auto(image, contrast_factor=3.0, baseline_brightness=1.3, target_white=245):
    """
    Convert the image to true grayscale, then boost contrast and automatically
    adjust brightness to make page background near white.
    """
    # Convert to true grayscale first
    img = image.convert("L")  # 'L' mode = 8-bit grayscale

    # Step 1: contrast
    enhancer = ImageEnhance.Contrast(img)
    img = enhancer.enhance(contrast_factor)

    # Step 2: auto brightness
    factor = auto_brightness_factor(img.convert("RGB"), target_white=target_white)
    factor *= baseline_brightness
    factor = min(factor, 2.0)

    enhancer = ImageEnhance.Brightness(img)
    img = enhancer.enhance(factor)

    # Convert back to RGB so PDF writer handles it consistently
    img = img.convert("RGB")
    return img

###############################################################################
# Book class
###############################################################################

class Book:
    def __init__(self, book_id):
        self.book_id = str(book_id)
        self.media_type = ""
        self.api_url = "https://api.nb.no/catalog/v1/iiif/URN:NBN:no-nb"
        self.page_names = []
        self.page_data = {}
        self.page_url = {}
        self.num_pages = 0
        self.title = self.book_id
        self.tile_width = 1024
        self.tile_height = 1024
        self.session = requests.Session()
        self.session.headers["User-Agent"] = "Mozilla/5.0"

    def set_media_type(self, media_type):
        self.media_type = media_type

    def set_tile_sizes(self):
        if self.media_type in ("digibok", "digitidsskrift"):
            self.tile_width = self.tile_height = 1024
        else:
            self.tile_width = self.tile_height = 4096

    def get_manifest(self):
        url = f"{self.api_url}_{self.media_type}_{self.book_id}/manifest"
        r = self.session.get(url)
        r.raise_for_status()
        data = r.json()

        if "label" in data:
            label = data["label"]
            if isinstance(label, list):
                self.title = str(label[0])
            elif isinstance(label, dict):
                self.title = list(label.values())[0][0]
            else:
                self.title = str(label)

        canvases = data["sequences"][0]["canvases"]
        for page in canvases:
            if self.media_type == "digavis":
                name = page["@id"].split("_")[-2]
            elif self.media_type == "digikart":
                name = page["@id"].split("_")[-2] + "_" + page["@id"].split("_")[-1]
            else:
                name = page["@id"].split("_")[-1]
            self.page_names.append(name)
            self.page_data[name] = (page["width"], page["height"])
            self.page_url[name] = page["images"][0]["resource"]["service"]["@id"]

        self.num_pages = len(self.page_names)
        if self.media_type == "digibok":
            self.num_pages -= 5

    def page_grid(self, page_name):
        self.set_tile_sizes()
        w, h = self.page_data[page_name]
        max_col = ceil(w / self.tile_width)
        max_row = ceil(h / self.tile_height)
        return max_col, max_row

    def tile_url(self, page, col, row):
        return (
            f"{self.page_url[page]}/"
            f"{col*self.tile_width},{row*self.tile_height},"
            f"{self.tile_width},{self.tile_height}"
            f"/full/0/native.jpg"
        )

###############################################################################
# Download page
###############################################################################

def download_page(page_name, book, out_path):
    if os.path.exists(out_path):
        return out_path

    page_width, page_height = book.page_data[page_name]
    max_col, max_row = book.page_grid(page_name)
    full_page = Image.new("RGB", (page_width, page_height))

    try:
        for row in range(max_row):
            for col in range(max_col):
                x = col * book.tile_width
                y = row * book.tile_height
                url = book.tile_url(page_name, col, row)
                with book.session.get(url, stream=True) as r:
                    r.raise_for_status()
                    with Image.open(r.raw) as tile:
                        tile.load()
                        full_page.paste(tile, (x, y))
    finally:
        full_page.save(out_path)
        full_page.close()
    print(f"✅ Lagret {os.path.basename(out_path)}")
    return out_path

###############################################################################
# Incremental PDF
###############################################################################

class IncrementalPDF:
    def __init__(self, pdf_path):
        self.pdf_path = pdf_path
        self.writer = PdfWriter()

    def build_from_images(self, images):
        for img_path in images:
            with Image.open(img_path) as img:
                pdf_bytes = image_to_pdf_bytes(img)
            reader = PdfReader(io.BytesIO(pdf_bytes))
            self.writer.add_page(reader.pages[0])
        with open(self.pdf_path, "wb") as f:
            self.writer.write(f)

###############################################################################
# Main
###############################################################################

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--id", required=True, help="ID på mediet")
    parser.add_argument("--start", type=int, default=1)
    parser.add_argument("--stop", type=int, default=None)
    args = parser.parse_args()

    media_type = "dig" + args.id.split("dig")[1].split("_")[0]
    media_id = args.id.split(media_type + "_")[1]

    # Initialize book
    book = Book(media_id)
    book.set_media_type(media_type)
    book.get_manifest()

    # Folder
    title = sanitize_filename(book.title) or media_id
    base_dir = os.path.join(os.path.expanduser("~/Downloads"), title)
    os.makedirs(base_dir, exist_ok=True)

    # Determine last downloaded page
    last_downloaded_idx = -1
    for i in reversed(range(len(book.page_names))):
        img_path = os.path.join(base_dir, f"{book.page_names[i]}.jpg")
        if os.path.exists(img_path):
            last_downloaded_idx = i
            break

    start_idx = max(args.start - 1, last_downloaded_idx + 1)
    stop_idx = args.stop or book.num_pages

    # Download missing pages
    for i in range(start_idx, stop_idx):
        page_name = book.page_names[i]
        img_path = os.path.join(base_dir, f"{page_name}.jpg")
        download_page(page_name, book, img_path)

    # Collect all downloaded images
    all_images = [os.path.join(base_dir, f"{name}.jpg") for name in book.page_names if os.path.exists(os.path.join(base_dir, f"{name}.jpg"))]

    # Build Color PDF
    pdf_path = os.path.join(base_dir, f"{title}.pdf")
    pdf = IncrementalPDF(pdf_path)
    pdf.build_from_images(all_images)
    print(f"📄 Color PDF ferdig: {pdf_path}")

    # Build enhanced grayscale PDF (_BW.pdf)
    pdf_bw_path = os.path.join(base_dir, f"{title}_BW.pdf")
    bw_images = []

    for img_path in all_images:
        with Image.open(img_path) as img:
            if is_primarily_grayscale(img):
                img_enhanced = enhance_grayscale_auto(
                    img,
                    contrast_factor=3.0,
                    baseline_brightness=1.3,
                    target_white=245
                )
            else:
                img_enhanced = img.copy()
            tmp_path = img_path + "_tmp_bw.jpg"
            img_enhanced.save(tmp_path)
            bw_images.append(tmp_path)
            img_enhanced.close()

    pdf_bw = IncrementalPDF(pdf_bw_path)
    pdf_bw.build_from_images(bw_images)

    # Cleanup temporary BW files
    for tmp in bw_images:
        os.remove(tmp)

    print(f"📄 Enhanced Grayscale PDF (_BW) ferdig: {pdf_bw_path}")
    print("🎉 Ferdig.")

if __name__ == "__main__":
    main()
