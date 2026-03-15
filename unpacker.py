import os
import struct
import subprocess
import array

# --- Configuration ---
BASE_DIRECTORY = os.path.dirname(os.path.abspath(__file__))
# ---------------------

def unswizzle_data(data, width, height, bpp):
    bytes_per_pixel = bpp // 8
    if width % 4 != 0:
        return data
    bytes_per_row = width * bytes_per_pixel
    quadrant_width_bytes = (width // 4) * bytes_per_pixel
    unswizzled_data = bytearray()
    for y in range(height):
        row_start = y * bytes_per_row
        row_data = data[row_start : row_start + bytes_per_row]
        chunk_C = row_data[0 : quadrant_width_bytes]
        chunk_D = row_data[quadrant_width_bytes : quadrant_width_bytes * 2]
        chunk_A = row_data[quadrant_width_bytes * 2 : quadrant_width_bytes * 3]
        chunk_B = row_data[quadrant_width_bytes * 3 : quadrant_width_bytes * 4]
        unswizzled_data.extend(chunk_A)
        unswizzled_data.extend(chunk_B)
        unswizzled_data.extend(chunk_C)
        unswizzled_data.extend(chunk_D)
    return bytes(unswizzled_data)

def unswizzle_horizontal_wrap(data, width, height, bpp):
    bytes_per_pixel = bpp // 8
    bytes_per_row = width * bytes_per_pixel
    half_row_bytes = (width // 2) * bytes_per_pixel
    corrected_data = bytearray()
    for y in range(height):
        row_start = y * bytes_per_row
        row_data = data[row_start : row_start + bytes_per_row]
        right_half = row_data[0 : half_row_bytes]
        left_half  = row_data[half_row_bytes : bytes_per_row]
        corrected_data.extend(left_half)
        corrected_data.extend(right_half)
    return bytes(corrected_data)

def reverse_rows(data, width, height, bpp):
    bytes_per_pixel = bpp // 8
    bytes_per_row = width * bytes_per_pixel
    out = bytearray()
    for y in range(height):
        row = data[y*bytes_per_row:(y+1)*bytes_per_row]
        # reverse order of pixels (not bytes)
        for i in range(bytes_per_row - bytes_per_pixel, -bytes_per_pixel, -bytes_per_pixel):
            out.extend(row[i:i+bytes_per_pixel])
    return bytes(out)

def swap_endianness_words_16(data):
    """Swap 16-bit word endianness. Disabled by default."""
    a = array.array('H', data)
    a.byteswap()
    return a.tobytes()

def write_bmp_16bpp_bi_rgb_topdown(path, width, height, pixel_data):
    """Write a top‑down 16‑bpp BI_RGB BMP (X1R5G5B5), with proper row padding."""
    bpp = 16
    rowbytes = (width * bpp) // 8
    stride   = ((width * bpp + 31) // 32) * 4
    rowpad   = stride - rowbytes
    image_sz = stride * height

    # File header
    bfType = b'BM'
    bfOffBits = 14 + 40  # no masks, no palette
    bfSize = bfOffBits + image_sz
    bmp_file_header = struct.pack('<2sIHHI', bfType, bfSize, 0, 0, bfOffBits)

    # DIB header (BITMAPINFOHEADER)
    biSize = 40
    biWidth = width
    biHeight = -height  # top-down
    biPlanes = 1
    biBitCount = 16
    biCompression = 0  # BI_RGB
    biSizeImage = image_sz
    biXPelsPerMeter = 2835
    biYPelsPerMeter = 2835
    biClrUsed = 0
    biClrImportant = 0
    dib = struct.pack('<IiiHHIIIIII',
                      biSize, biWidth, biHeight, biPlanes, biBitCount,
                      biCompression, biSizeImage, biXPelsPerMeter, biYPelsPerMeter,
                      biClrUsed, biClrImportant)

    with open(path, 'wb') as f:
        f.write(bmp_file_header)
        f.write(dib)
        # write rows with padding
        for y in range(height):
            start = y * rowbytes
            f.write(pixel_data[start:start+rowbytes])
            if rowpad:
                f.write(b'\x00' * rowpad)

def write_bmp_24bpp_bi_rgb_topdown(path, width, height, pixel_data):
    """Write a top‑down 24‑bpp BI_RGB BMP (BGR order on disk), with padding."""
    bpp = 24
    rowbytes = (width * bpp) // 8
    stride   = ((width * bpp + 31) // 32) * 4
    rowpad   = stride - rowbytes
    image_sz = stride * height

    bfType = b'BM'
    bfOffBits = 14 + 40
    bfSize = bfOffBits + image_sz
    bmp_file_header = struct.pack('<2sIHHI', bfType, bfSize, 0, 0, bfOffBits)

    biSize = 40
    biWidth = width
    biHeight = -height  # top-down
    biPlanes = 1
    biBitCount = 24
    biCompression = 0  # BI_RGB
    biSizeImage = image_sz
    biXPelsPerMeter = 2835
    biYPelsPerMeter = 2835
    biClrUsed = 0
    biClrImportant = 0
    dib = struct.pack('<IiiHHIIIIII',
                      biSize, biWidth, biHeight, biPlanes, biBitCount,
                      biCompression, biSizeImage, biXPelsPerMeter, biYPelsPerMeter,
                      biClrUsed, biClrImportant)

    with open(path, 'wb') as f:
        f.write(bmp_file_header)
        f.write(dib)
        for y in range(height):
            start = y * rowbytes
            f.write(pixel_data[start:start+rowbytes])
            if rowpad:
                f.write(b'\x00' * rowpad)

# --- Main Script Logic ---
if __name__ == '__main__':
    tga_folder = os.path.join(BASE_DIRECTORY, "TGA")
    dec_folder = os.path.join(BASE_DIRECTORY, "TGADEC")
    bmp_folder = os.path.join(BASE_DIRECTORY, "BMP")

    quickbms_exe = os.path.join(BASE_DIRECTORY, "quickbms.exe")
    bms_script   = os.path.join(BASE_DIRECTORY, "tga.bms")

    os.makedirs(dec_folder, exist_ok=True)
    os.makedirs(bmp_folder, exist_ok=True)

    print("--- Step 1: Decompressing all .TGA files ---")
    tga_files = [f for f in os.listdir(tga_folder) if f.upper().endswith(".TGA")]
    for filename in tga_files:
        input_path = os.path.join(tga_folder, filename)
        extra = os.environ.get("QBM_FLAGS", "").split()
        command = [quickbms_exe] + extra + [bms_script, input_path, dec_folder]
        subprocess.run(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    print("Decompression complete.\\n")

    print("--- Step 2: Building all .BMP files ---")
    dec_files = [f for f in os.listdir(dec_folder) if f.upper().endswith(".TGA.DEC")]
    for dec_filename in dec_files:
        try:
            original_tga_name = dec_filename.replace(".dec", "")
            original_tga_path = os.path.join(tga_folder, original_tga_name)

            # TGA stub: read (unc_size, width, height) at offset 4
            with open(original_tga_path, 'rb') as f_in:
                f_in.seek(4)
                unc_size, width, height = struct.unpack('<III', f_in.read(12))

            dec_filepath = os.path.join(dec_folder, dec_filename)
            with open(dec_filepath, 'rb') as f_raw:
                pixel_data = f_raw.read()

            # Determine bpp from reported uncompressed size
            if unc_size == width * height * 2:
                bpp = 16
            elif unc_size == width * height * 3:
                bpp = 24
            else:
                print(f"Skipping {dec_filename}: Unknown BPP (unc={unc_size}).")
                continue

            print(f"Processing {dec_filename} ({width}x{height} @ {bpp}bpp)...")

            # --- Geometry pipeline (as before) ---
            pixel_data = unswizzle_data(pixel_data, width, height, bpp)
            pixel_data = unswizzle_horizontal_wrap(pixel_data, width, height, bpp)
            #pixel_data = reverse_rows(pixel_data, width, height, bpp)

            # IMPORTANT: Do NOT swap endianness for 16-bpp; data is already little-endian.
            # If you hit obviously wrong colours after all fixes, toggle this once to test:
            SWAP_ENDIAN_16BPP = False
            if bpp == 16 and SWAP_ENDIAN_16BPP:
                pixel_data = swap_endianness_words_16(pixel_data)

            output_bmp_name = original_tga_name.replace(".TGA", ".bmp")
            output_bmp_path = os.path.join(bmp_folder, output_bmp_name)

            if bpp == 16:
                write_bmp_16bpp_bi_rgb_topdown(output_bmp_path, width, height, pixel_data)
            else:  # 24 bpp
                write_bmp_24bpp_bi_rgb_topdown(output_bmp_path, width, height, pixel_data)

        except Exception as e:
            print(f"Could not process {dec_filename}: {e}")

    print("BMP building complete.\\n")

    print("--- Step 3: Cleaning up intermediate files ---")
    for dec_filename in dec_files:
        try:
            os.remove(os.path.join(dec_folder, dec_filename))
        except FileNotFoundError:
            pass
    try:
        os.rmdir(dec_folder)
    except OSError:
        pass
    print("Cleanup complete.")

    print("\\n--- All done! ---")
