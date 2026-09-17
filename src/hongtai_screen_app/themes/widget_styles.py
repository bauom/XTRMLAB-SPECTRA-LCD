"""Shared widget styles. Geometry is drawn locally at 2x for clean LCD edges.

Defaults are merged before rendering; element fields remain the final overrides.
This module has no hardware or application state and never modifies saved data.
"""

import math

from PIL import Image, ImageDraw, ImageOps


STYLES = {
    "gothic": {
        "label": "Gothic / engraved metal",
        "defaults": {
            "font": "cinzel", "color": (164, 42, 58),
            "text_color": (231, 220, 203), "ornament_color": (155, 143, 133),
            "muted_color": (175, 158, 152), "track_color": (35, 28, 33),
            "face_color": (13, 10, 15), "alert_color": (226, 65, 76),
        },
    },
    "cyberpunk": {
        "label": "Cyberpunk / angular HUD",
        "defaults": {
            "font": "orbitron", "color": (33, 224, 238),
            "text_color": (222, 246, 255), "ornament_color": (57, 141, 169),
            "muted_color": (111, 184, 201), "track_color": (17, 39, 54),
            "face_color": (6, 17, 28), "alert_color": (255, 106, 55),
        },
    },
    "high_fantasy": {
        "label": "High Fantasy / arcane instruments",
        "defaults": {
            "font": "cinzel", "color": (60, 186, 140),
            "text_color": (245, 231, 191), "ornament_color": (191, 157, 88),
            "muted_color": (177, 178, 145), "track_color": (23, 47, 41),
            "face_color": (9, 23, 22), "alert_color": (247, 142, 67),
        },
    },
}


def resolve_element(el, background=None):
    style = el.get("widget_style") or (background or {}).get("widget_style", "default")
    if style not in STYLES:
        return el
    defaults = dict(STYLES[style]["defaults"])
    if el.get("type") in ("text", "clock"):
        defaults["color"] = defaults["text_color"]
    defaults.update({key: value for key, value in el.items() if value is not None})
    defaults["widget_style"] = style
    return defaults


def color(el, key):
    style = el.get("widget_style", "gothic")
    return tuple(el.get(key) or STYLES.get(style, STYLES["gothic"])["defaults"][key])


def _paste(img, tile, xy, opacity=1):
    if opacity < 1:
        tile.putalpha(tile.getchannel("A").point(lambda a: round(a * max(0, opacity))))
    img.paste(tile, xy, tile)


def diamond(draw, x, y, r, fill, outline=None):
    draw.polygon([(x, y-r), (x+r, y), (x, y+r), (x-r, y)], fill=fill, outline=outline)


def rule(draw, x0, x1, y, ink, style="gothic"):
    mid = (x0 + x1) / 2
    if style == "cyberpunk":
        draw.line([(x0,y),(mid-6,y),(mid,y-3),(mid+6,y),(x1,y)],fill=ink,width=1)
        for x in (x0,x1):
            draw.line([(x,y-2),(x,y+2)],fill=ink,width=1)
        return
    draw.line([(x0, y), (mid-8, y)], fill=ink, width=1)
    draw.line([(mid+8, y), (x1, y)], fill=ink, width=1)
    diamond(draw, mid, y, 4, ink)
    diamond(draw, x0, y, 2, ink)
    diamond(draw, x1, y, 2, ink)


def draw_frame(img, ink, style="gothic"):
    """Distinct perimeter geometry for each style, scaled to the panel."""
    w, h = img.size
    s = min(w / 960, h / 480)
    layer = Image.new("RGBA", img.size)
    d = ImageDraw.Draw(layer)
    m, length = max(7, round(14*s)), round(36*s)
    ink = tuple(ink)
    if style == "cyberpunk":
        for x in range(0,w,max(1,round(44*s))):
            d.line((x,0,x,h),fill=(33,100,140,45))
        for y in range(0,h,max(1,round(44*s))):
            d.line((0,y,w,y),fill=(33,100,140,45))
        c = 18*s
        points = [(m+c,m),(w-m-c,m),(w-m,m+c),(w-m,h-m-c),
                  (w-m-c,h-m),(m+c,h-m),(m,h-m-c),(m,m+c),(m+c,m)]
        d.line(points, fill=(*ink, 210), width=max(1,round(s)))
        for x, dx in ((m,1),(w-m,-1)):
            for y, dy in ((m,1),(h-m,-1)):
                d.line([(x,y+dy*54*s),(x,y+dy*22*s),(x+dx*22*s,y),(x+dx*70*s,y)],
                       fill=(*ink,255), width=max(2,round(3*s)))
        for i in range(5):
            d.line([(w*.46+i*15*s,h-m),(w*.46+i*15*s+8*s,h-m)], fill=(*ink,255), width=3)
        img.paste(layer,(0,0),layer)
        return
    if style == "high_fantasy":
        # A faint astrolabe and a few fixed stars behind the live content.
        for radius in (170*s,178*s):
            d.ellipse((w/2-radius,h*.48-radius,w/2+radius,h*.48+radius),outline=(*ink,32))
        for fx,fy in ((.32,.20),(.68,.20),(.32,.74),(.68,.74),(.36,.52),(.64,.52)):
            diamond(d,w*fx,h*fy,2*s,(*ink,100))
        d.rounded_rectangle((m,m,w-m-1,h-m-1), radius=24*s, outline=(*ink,160), width=max(1,round(s)))
        d.rounded_rectangle((m+5*s,m+5*s,w-m-1-5*s,h-m-1-5*s), radius=20*s, outline=(*ink,75))
        for x, dx in ((m,1),(w-m-1,-1)):
            for y, dy in ((m,1),(h-m-1,-1)):
                # Curling vines with small gold leaves in each corner.
                for j in range(3):
                    px,py=x+dx*(13+j*9)*s,y+dy*(35-j*9)*s
                    d.ellipse((px-4*s,py-6*s,px+4*s,py+6*s), outline=(*ink,220))
                d.line([(x,y+dy*52*s),(x+dx*15*s,y+dy*30*s),(x+dx*30*s,y+dy*15*s),(x+dx*52*s,y)], fill=(*ink,180))
        rule(d,w*.4,w*.6,h*.965,(*ink,210))
        diamond(d,w/2,h*.965,5*s,STYLES[style]["defaults"]["color"],ink)
        img.paste(layer,(0,0),layer)
        return
    d.rectangle((m, m, w-m-1, h-m-1), outline=(*ink, 150))
    for x, dx in ((m, 1), (w-m-1, -1)):
        for y, dy in ((m, 1), (h-m-1, -1)):
            d.line([(x, y+dy*length), (x+dx*7*s, y+dy*10*s),
                    (x+dx*length, y)], fill=(*ink, 240), width=max(1, round(s)))
            d.line([(x+dx*4*s, y+dy*length), (x+dx*11*s, y+dy*17*s),
                    (x+dx*length, y+dy*4*s)], fill=(*ink, 120))
            diamond(d, x+dx*12*s, y+dy*12*s, 3*s, (*ink, 230))
    rule(d, w*.39, w*.61, h*.965, (*ink, 180))
    img.paste(layer, (0, 0), layer)


def meter_tile(w, h, fraction, el):
    """Pointed metal channel; unknown values show an empty track."""
    w, h = max(2, round(w)), max(3, round(h))
    scale = 2
    W, H = w*scale, h*scale
    tile = Image.new("RGBA", (W, H))
    d = ImageDraw.Draw(tile)
    edge, track, accent = color(el, "ornament_color"), color(el, "track_color"), color(el, "color")
    style = el.get("widget_style", "gothic")
    tip = min(5 if style == "cyberpunk" else H/2, W/6)
    points = [(0, H/2), (tip, 1), (W-tip-1, 1), (W-1, H/2), (W-tip-1, H-2), (tip, H-2)]
    d.polygon(points, fill=track)
    if fraction is not None and fraction > 0:
        mask = Image.new("L", tile.size)
        ImageDraw.Draw(mask).polygon(points, fill=255)
        fill = Image.new("RGBA", tile.size)
        f = ImageDraw.Draw(fill)
        amount = max(1, round((W-1)*min(1, fraction)))
        f.rectangle((0, 2, amount, H-3), fill=accent)
        f.line((0, H/2-1, amount, H/2-1), fill=tuple(min(255, int(c*1.2)) for c in accent), width=2)
        fill.putalpha(Image.composite(fill.getchannel("A"), Image.new("L", tile.size), mask))
        tile.alpha_composite(fill)
    d = ImageDraw.Draw(tile)
    d.line(points + [points[0]], fill=edge, width=2)
    if style != "high_fantasy":
        for i in range(1, 10):
            x = W*i/10
            d.line((x, 3, x, H-4), fill=(*track, 255), width=4 if style == "cyberpunk" else 2)
    else:
        for x in (tip, W-tip-1):
            diamond(d,x,H/2,max(1,H*.22),accent,edge)
    return tile.resize((w, h), Image.Resampling.LANCZOS)


def draw_bar(img, el, box, value, stat, font_loader):
    vertical = el.get("orientation") == "vertical"
    length = box["h"] if vertical else box["w"]
    thickness = min(14, box["w"] if vertical else box["h"])
    fraction = None if value is None else max(0, min(1, (value-stat["min"])/max(1e-6, stat["max"]-stat["min"])))
    tile = meter_tile(length, thickness, fraction, el)
    if vertical:
        tile = tile.transpose(Image.Transpose.ROTATE_90)
    _paste(img, tile, (round(box["cx"]-tile.width/2), round(box["cy"]-tile.height/2)), el.get("opacity", 1))
    if el.get("show_value", True):
        label = "--" if value is None else stat["fmt"](value)
        layer = Image.new("RGBA", img.size)
        ImageDraw.Draw(layer).text((box["cx"], box["y0"]-3), label,
            font=font_loader(13, el.get("bold", True), el.get("font")), fill=color(el, "text_color"), anchor="mb")
        _paste(img, layer, (0, 0), el.get("opacity", 1))


def _dial_canvas(g):
    radius = g["radius"]
    size = math.ceil(radius*2 + 24)
    return Image.new("RGBA", (size*2, size*2)), size, size, radius*2


def _point(cx, cy, r, degrees):
    a = math.radians(degrees)
    return (cx + math.cos(a)*r, cy + math.sin(a)*r)


def draw_gauge_static(img, el, g, title, font_loader):
    tile, cx, cy, r = _dial_canvas(g)
    d = ImageDraw.Draw(tile)
    metal, muted = color(el, "ornament_color"), color(el, "muted_color")
    style = el.get("widget_style", "gothic")
    if style == "cyberpunk":
        bezel = [_point(cx,cy,r,22.5+i*45) for i in range(8)]
        d.polygon(bezel,fill=color(el,"face_color"),outline=metal,width=2)
        for start in (135,230,325):
            d.arc((cx-r*.96,cy-r*.96,cx+r*.96,cy+r*.96),start,start+75,fill=color(el,"color"),width=3)
    else:
        d.ellipse((cx-r, cy-r, cx+r, cy+r), fill=color(el, "face_color"), outline=metal, width=2)
        d.ellipse((cx-r*.95, cy-r*.95, cx+r*.95, cy+r*.95), outline=(*metal, 100), width=2)
    d.arc((cx-r*.81, cy-r*.81, cx+r*.81, cy+r*.81), 135, 405, fill=color(el, "track_color"), width=7)
    # Symmetric metalwork surrounds the dial; ticks retain real metric values.
    for angle in (range(0,360,45) if style == "high_fantasy" else (0, 90, 180, 270)):
        px, py = _point(cx, cy, r+5, angle)
        if style == "cyberpunk":
            d.rectangle((px-3,py-3,px+3,py+3),fill=color(el,"color"))
        else:
            diamond(d, px, py, 5, color(el, "color"), metal)
    for i in range(21):
        a = 135+i*13.5
        major = i % 5 == 0
        d.line([_point(cx, cy, r*(.76 if major else .83), a), _point(cx, cy, r*.91, a)],
               fill=metal if major else (*metal, 115), width=2)
    labels = ((10, "50"),) if style == "cyberpunk" else ((0, "0"), (10, "50"), (20, "100"))
    for i, label in labels:
        d.text(_point(cx, cy, r*.64, 135+i*13.5), label,
               font=font_loader(max(15, round(r*.13)), False, el.get("font")), fill=muted, anchor="mm")
    if style == "high_fantasy":
        for a in range(0,360,60):
            p=_point(cx,cy,r*.2,a)
            d.ellipse((p[0]-r*.13,p[1]-r*.13,p[0]+r*.13,p[1]+r*.13),outline=(*metal,70),width=2)
    elif style == "gothic":
        d.line([(cx-9, cy-10), (cx, cy-23), (cx+9, cy-10)], fill=(*metal, 140), width=2)
    tile = tile.resize((tile.width//2, tile.height//2), Image.Resampling.LANCZOS)
    _paste(img, tile, (round(g["cx"]-tile.width/2), round(g["cy"]-tile.height/2)), el.get("opacity", 1))
    if el.get("show_title", True):
        layer = Image.new("RGBA", img.size)
        d = ImageDraw.Draw(layer)
        d.text((g["cx"], g["cy"]-g["radius"]-12), title,
               font=font_loader(max(10, round(g["radius"]*.18)), el.get("bold", True), el.get("font")),
               fill=color(el, "text_color"), anchor="mm")
        _paste(img, layer, (0, 0), el.get("opacity", 1))


def draw_gauge_dynamic(img, el, g, value, stat, font_loader):
    tile, cx, cy, r = _dial_canvas(g)
    d = ImageDraw.Draw(tile)
    accent = color(el, "color")
    fraction = None if value is None else max(0, min(1, (value-stat["min"])/max(1e-6, stat["max"]-stat["min"])))
    if fraction is not None:
        if el.get("stat") in ("cpu_load", "gpu_load", "ram", "vram_usage", "gpu_temp") and fraction >= .9:
            accent = color(el, "alert_color")
        if fraction > 0:
            d.arc((cx-r*.81, cy-r*.81, cx+r*.81, cy+r*.81), 135, 135+270*fraction, fill=accent, width=4)
        angle = 135+270*fraction
        tip = _point(cx, cy, r*.75, angle)
        shoulder = _point(cx, cy, r*.45, angle)
        left = _point(*shoulder, 4, angle+90)
        right = _point(*shoulder, 4, angle-90)
        tail = _point(cx, cy, r*.18, angle+180)
        if el.get("widget_style") == "cyberpunk":
            d.line([(cx,cy),tip],fill=accent,width=3)
            d.polygon([tip,left,right],fill=color(el,"text_color"))
        else:
            d.polygon([tip, left, tail, right], fill=color(el, "text_color"))
            d.line([tail, tip], fill=accent, width=2)
    if el.get("widget_style") == "cyberpunk":
        d.rectangle((cx-5,cy-5,cx+5,cy+5),fill=accent)
    else:
        diamond(d, cx, cy, 7, accent, color(el, "ornament_color"))
    value_text = "--" if value is None else stat["fmt"](value)
    d.text((cx, cy+r*.46), value_text,
           font=font_loader(max(20, round(r*.28)), el.get("bold", True), el.get("font")),
           fill=color(el, "text_color"), anchor="mm")
    tile = tile.resize((tile.width//2, tile.height//2), Image.Resampling.LANCZOS)
    _paste(img, tile, (round(g["cx"]-tile.width/2), round(g["cy"]-tile.height/2)), el.get("opacity", 1))


def draw_media(img, el, box, media, font_loader, idle_art, idle_message):
    """Square cover with a styled surround; all drawing is clipped to the box.

    Cover art is never cropped into an arch. Only its ornamental surround is
    pointed, so album text and artwork remain visible. Progress uses the meter
    shared with system bars and disappears when duration is unavailable.
    """
    w, h = max(1, round(box["w"])), max(1, round(box["h"]))
    s = 2
    tile = Image.new("RGBA", (w*s, h*s))
    d = ImageDraw.Draw(tile)
    media = media or {}
    title, artist = media.get("title"), media.get("artist")
    duration, position = media.get("duration"), media.get("position") or 0
    show_art, show_name = el.get("show_art", True), el.get("show_name", True)
    show_time = el.get("show_time", True) and bool(title and duration and duration > 0)
    font = font_loader(16*s, el.get("bold", False), el.get("font"))
    small = font_loader(12*s, False, el.get("font"))
    tiny = font_loader(11*s, False, el.get("font"))
    metal, text = color(el, "ornament_color"), color(el, "text_color")

    def fit_line(label, face, available):
        label = str(label)
        if d.textlength(label, font=face) <= available:
            return label
        while label and d.textlength(label + "…", font=face) > available:
            label = label[:-1]
        return label + "…"

    style = el.get("widget_style", "gothic")
    frame_space = 28 if style == "cyberpunk" else 48
    rows = (42 if show_name else 0) + (32 if show_time else 0)
    art_size = max(0, min(w-38, h-rows-frame_space)) if show_art else 0
    content_h = (art_size+frame_space if show_art and art_size > 0 else 0) + rows
    y = max(0, (h-content_h)/2)*s
    cx = w*s/2
    if show_art and art_size > 0:
        a = art_size*s
        x0, top = (w*s-a)/2, y+(frame_space-14)*s
        # Lancet arch and inset, with a square, unaltered cover below.
        def curve(start, control, end):
            return [tuple((1-t)**2*start[k] + 2*(1-t)*t*control[k] + t*t*end[k] for k in (0,1))
                    for t in (i/20 for i in range(21))]
        arch = [(x0-6*s, top+a+6*s)]
        arch += curve((x0-6*s, top+6*s), (x0-6*s, y+12*s), (cx, y))
        arch += curve((cx, y), (x0+a+6*s, y+12*s), (x0+a+6*s, top+6*s))
        arch += [(x0+a+6*s, top+a+6*s)]
        if style == "gothic":
            d.line(arch, fill=metal, width=2*s)
        inset = curve((x0-2*s, top-2*s), (x0+4*s, y+17*s), (cx, y+6*s))
        inset += curve((cx, y+6*s), (x0+a-4*s, y+17*s), (x0+a+2*s, top-2*s))
        if style == "gothic":
            d.line(inset, fill=color(el, "color"), width=s)
            diamond(d, cx, y+2*s, 3*s, color(el, "color"), metal)
        elif style == "cyberpunk":
            for px,dx in ((x0-5*s,1),(x0+a+5*s,-1)):
                for py,dy in ((top-5*s,1),(top+a+5*s,-1)):
                    d.line([(px,py+dy*22*s),(px,py),(px+dx*22*s,py)],fill=color(el,"color"),width=2*s)
        else:
            d.rectangle((x0-5*s,top-5*s,x0+a+5*s,top+a+5*s),outline=metal,width=s)
            d.rectangle((x0-8*s,top-8*s,x0+a+8*s,top+a+8*s),outline=(*metal,100),width=s)
            for px in (x0-5*s,x0+a+5*s):
                for py in (top-5*s,top+a+5*s):
                    diamond(d,px,py,4*s,color(el,"color"),metal)
            crest=[_point(cx,y+12*s,(10 if i%2 == 0 else 4)*s,-90+i*45) for i in range(8)]
            d.polygon(crest,fill=color(el,"color"),outline=metal,width=s)
        art = media.get("art")
        if art is None:
            art = idle_art
        if art is not None:
            cover = ImageOps.fit(art.convert("RGB"), (a, a), method=Image.Resampling.LANCZOS)
            tile.paste(cover, (round(x0), round(top)))
        else:
            d.rectangle((x0, top, x0+a, top+a), fill=color(el, "face_color"))
            for radius in (a*.29, a*.25, a*.06):
                d.ellipse((cx-radius, top+a/2-radius, cx+radius, top+a/2+radius), outline=metal, width=s)
            diamond(d, cx, top+a/2, 3*s, color(el, "color"))
        d.rectangle((x0-s, top-s, x0+a, top+a), outline=metal, width=s)
        y = top+a+14*s
    if show_name:
        d.text((cx, y), fit_line(title or idle_message, font, (w-12)*s), font=font, fill=text, anchor="mt")
        y += 23*s
        if artist:
            d.text((cx, y), fit_line(artist, small, (w-12)*s), font=small, fill=color(el, "muted_color"), anchor="mt")
        y += 19*s
    if show_time:
        bar_w = max(2, w-24)
        meter = meter_tile(bar_w*s, 7*s, max(0, min(1, position/duration)), el)
        tile.alpha_composite(meter, (12*s, round(y)))
        y += 13*s
        fmt = lambda seconds: f"{int(max(0, seconds))//60}:{int(max(0, seconds))%60:02d}"
        d.text((12*s, y), fmt(position), font=tiny, fill=color(el, "muted_color"), anchor="lt")
        d.text(((w-12)*s, y), fmt(duration), font=tiny, fill=color(el, "muted_color"), anchor="rt")
    tile = tile.resize((w, h), Image.Resampling.LANCZOS)
    _paste(img, tile, (round(box["x0"]), round(box["y0"])), el.get("opacity", 1))
