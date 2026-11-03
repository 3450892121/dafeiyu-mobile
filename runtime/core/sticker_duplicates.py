"""Local image identity checks shared by upload, cleanup and candidate diversity.

The small hash only finds possible matches. Recycling requires decoded pixels or
a detailed image comparison; animations are compared through every frame.
"""
import hashlib, json, os, tempfile

VERSION = 3


def _rgb(im, trim=True):
    from PIL import Image, ImageChops, ImageOps
    im = ImageOps.exif_transpose(im).convert('RGBA')
    bg = Image.new('RGBA', im.size, 'white')
    bg.alpha_composite(im)
    im = bg.convert('RGB')
    if trim and min(im.size) > 2:
        corners = [im.getpixel(p) for p in [(0, 0), (im.width-1, 0), (0, im.height-1), (im.width-1, im.height-1)]]
        color = corners[0]
        if all(max(abs(a-b) for a, b in zip(color, c)) <= 6 for c in corners):
            diff = ImageChops.difference(im, Image.new('RGB', im.size, color)).convert('L')
            box = diff.point(lambda x: 255 if x > 6 else 0).getbbox()
            if box:
                im = im.crop(box)
    return im


def _dhash(im):
    from PIL import Image
    px = list(im.convert('L').resize((9, 8), Image.Resampling.LANCZOS).getdata())
    bits = 0
    for y in range(8):
        for x in range(8):
            bits = (bits << 1) | (px[y*9+x] > px[y*9+x+1])
    return '%016x' % bits


def distance(a, b):
    try:
        return (int(a, 16) ^ int(b, 16)).bit_count()
    except (TypeError, ValueError):
        return 65


def fingerprint(path):
    from PIL import Image
    stat = os.stat(path)
    h = hashlib.sha1()
    with open(path, 'rb') as fh:
        for block in iter(lambda: fh.read(1024*1024), b''):
            h.update(block)
    out = {'v': VERSION, 'stat': [stat.st_size, stat.st_mtime_ns], 'sha1': h.hexdigest()}
    try:
        with Image.open(path) as im:
            frames = getattr(im, 'n_frames', 1)
            out.update(frames=frames, width=im.width, height=im.height)
            im.seek(frames // 2 if frames > 1 else 0)
            rgb = _rgb(im, trim=frames == 1)
            out.update(dhash=_dhash(rgb), ratio=rgb.width / max(1, rgb.height))
            if frames == 1:
                out['pixels'] = hashlib.sha256(str(rgb.size).encode()+rgb.tobytes()).hexdigest()
    except Exception:
        # Exact byte matches are still useful for an unreadable image.
        out['dhash'] = ''
    return out


def load_hashes(sticker_dir, names, previous=None):
    path = os.path.join(sticker_dir, 'sticker_hash.json')
    old = previous
    if not isinstance(old, dict):
        try:
            with open(path, encoding='utf8') as fh:
                old = json.load(fh)
        except Exception:
            old = {}
    if not isinstance(old, dict):
        old = {}
    result = {}
    for name in sorted(set(names)):
        if not name or name != os.path.basename(name):
            continue
        fp = os.path.join(sticker_dir, 'images', name)
        try:
            st = os.stat(fp)
            cached = old.get(name)
            if isinstance(cached, dict) and cached.get('v') == VERSION and cached.get('stat') == [st.st_size, st.st_mtime_ns]:
                result[name] = cached
            else:
                result[name] = fingerprint(fp)
        except OSError:
            continue
    if result != old:
        fd, tmp = tempfile.mkstemp(prefix='.hash-', dir=sticker_dir)
        try:
            with os.fdopen(fd, 'w', encoding='utf8') as fh:
                json.dump(result, fh, ensure_ascii=False)
            os.replace(tmp, path)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)
    return result


def _close(a, b, size=192):
    from PIL import Image, ImageChops, ImageStat
    a = a.resize((size, size), Image.Resampling.LANCZOS)
    b = b.resize((size, size), Image.Resampling.LANCZOS)
    diff = ImageChops.difference(a, b)
    mean = sum(ImageStat.Stat(diff).mean)/3
    if mean > 3.0:
        return False
    # A small changed caption/face must not disappear into the whole-image mean.
    red, green, blue = diff.split()
    maximum = ImageChops.lighter(ImageChops.lighter(red, green), blue)
    high = sum(maximum.histogram()[41:])/(size*size)
    if high > .001:
        return False
    cell = size//8
    for y in range(0, size, cell):
        for x in range(0, size, cell):
            if max(ImageStat.Stat(diff.crop((x, y, x+cell, y+cell))).mean) > 4:
                return False
    return True


def same(a, b, path_a=None, path_b=None):
    """Return a reason only when a match is verified, otherwise an empty string."""
    if a.get('sha1') and a.get('sha1') == b.get('sha1'):
        return '文件完全相同'
    na, nb = a.get('frames', 1), b.get('frames', 1)
    if na != nb or not a.get('dhash') or not b.get('dhash'):
        return ''
    if na == 1 and a.get('pixels') and a.get('pixels') == b.get('pixels'):
        return '画面像素相同（格式或边框不同）'
    if distance(a['dhash'], b['dhash']) > 10:
        return ''
    ratio_error = abs(a.get('ratio', 1)/max(.001, b.get('ratio', 1))-1)
    canvas_error = abs((a.get('width', 1)/max(1, a.get('height', 1))) /
                       max(.001, b.get('width', 1)/max(1, b.get('height', 1)))-1)
    if min(ratio_error, canvas_error) > .015:
        return ''
    if not path_a or not path_b or na > 200:
        return ''
    from PIL import Image
    try:
        with Image.open(path_a) as ia, Image.open(path_b) as ib:
            for i in range(na):
                ia.seek(i); ib.seek(i)
                if na > 1 and abs(int(ia.info.get('duration', 100))-int(ib.info.get('duration', 100))) > 20:
                    return ''
                # JPEG ringing changes the inferred border by a few pixels.
                # Check the untrimmed canvas first so that a crop artefact cannot
                # turn an otherwise identical rescale into a shifted picture.
                if _close(_rgb(ia, trim=False), _rgb(ib, trim=False)):
                    continue
                if na == 1 and _close(_rgb(ia), _rgb(ib)):
                    continue
                return ''
        return '画面相同（缩放、压缩或另存）' if na == 1 else '动图逐帧相同'
    except Exception:
        return ''


def scan(items, hashes, images_dir, progress=None):
    """Keep annotated, higher-resolution originals. Never group through a removed file."""
    ordered = sorted(items, key=lambda x: (
        -bool(x.get('tags') or x.get('note')),
        -(hashes.get(x.get('file'), {}).get('width', 0)*hashes.get(x.get('file'), {}).get('height', 0)),
        x.get('added') or 0, x.get('file') or ''))
    kept, duplicates, seen = [], {}, set()
    for i, item in enumerate(ordered):
        name = item.get('file') or ''
        if not name or name in seen:
            continue
        seen.add(name)
        a = hashes.get(name) or {}
        for other in kept:
            why = same(a, hashes.get(other) or {}, os.path.join(images_dir, name), os.path.join(images_dir, other))
            if why:
                duplicates[name] = {'keep': other, 'why': why}
                break
        else:
            kept.append(name)
        if progress and (i % 20 == 0 or i+1 == len(ordered)):
            progress(i+1, len(ordered))
    return duplicates
