import React from "react";

// Defaults come from the same registry used by the Pillow renderer.
export function resolveWidgetStyle(el, background, styles) {
  const style = el.widget_style || background?.widget_style || "default";
  const spec = styles?.[style];
  if (!spec) return el;
  const defaults = { ...spec.defaults };
  if (["text", "clock"].includes(el.type)) defaults.color = defaults.text_color;
  const overrides = Object.fromEntries(Object.entries(el).filter(([, value]) => value != null));
  return { ...defaults, ...overrides, widget_style: style };
}

export function widgetFont(el) {
  return el.font === "unifraktur" ? '"Nocturne Blackletter", serif'
    : el.font === "cinzel" ? '"Nocturne Serif", serif'
    : el.font === "orbitron" ? '"Dashboard Orbitron", sans-serif' : undefined;
}

const rgb = (value) => `rgb(${value.join(",")})`;
const point = (cx, cy, r, angle) => {
  const a = angle * Math.PI / 180;
  return [cx + r * Math.cos(a), cy + r * Math.sin(a)];
};
const diamond = (x, y, r) => `${x},${y-r} ${x+r},${y} ${x},${y+r} ${x-r},${y}`;

function Meter({ x, y, w, h, el }) {
  const tip = Math.min(el.widget_style === "cyberpunk" ? 2.5 : h / 2, w / 6);
  const shape = `${x},${y+h/2} ${x+tip},${y} ${x+w-tip},${y} ${x+w},${y+h/2} ${x+w-tip},${y+h} ${x+tip},${y+h}`;
  const clip = `gothic_meter_${el.id}`;
  return (
    <g>
      <defs><clipPath id={clip}><polygon points={shape} /></clipPath></defs>
      <polygon points={shape} fill={rgb(el.track_color)} />
      <rect x={x} y={y+1} width={w*.42} height={Math.max(1,h-2)} fill={rgb(el.color)} clipPath={`url(#${clip})`} />
      {el.widget_style !== "high_fantasy" && Array.from({ length: 9 }, (_, i) => <line key={i} x1={x+w*(i+1)/10} x2={x+w*(i+1)/10}
        y1={y+1} y2={y+h-1} stroke={rgb(el.track_color)} strokeWidth={el.widget_style === "cyberpunk" ? 2 : 1} />)}
      <polygon points={shape} fill="none" stroke={rgb(el.ornament_color)} />
      {el.widget_style === "high_fantasy" && [x+tip,x+w-tip].map((px) => <polygon key={px}
        points={diamond(px,y+h/2,h*.22)} fill={rgb(el.color)} stroke={rgb(el.ornament_color)} />)}
    </g>
  );
}

export function StyledGaugePreview({ el, title, cx, cy, r }) {
  const metal = rgb(el.ornament_color);
  return (
    <g opacity={el.opacity ?? 1} style={{ pointerEvents: "none", fontFamily: widgetFont(el) }}>
      {el.widget_style === "cyberpunk" ? <>
        <polygon points={Array.from({length:8},(_,i)=>point(cx,cy,r,22.5+i*45).join(",")).join(" ")}
          fill={rgb(el.face_color)} stroke={metal} />
        {[135,230,325].map((a) => {
          const start=point(cx,cy,r*.96,a), end=point(cx,cy,r*.96,a+75);
          return <path key={a} d={`M ${start.join(" ")} A ${r*.96} ${r*.96} 0 0 1 ${end.join(" ")}`}
            fill="none" stroke={rgb(el.color)} strokeWidth={1.5} />;
        })}
      </> : <>
        <circle cx={cx} cy={cy} r={r} fill={rgb(el.face_color)} stroke={metal} />
        <circle cx={cx} cy={cy} r={r*.95} fill="none" stroke={metal} strokeOpacity={.4} />
      </>}
      {Array.from({ length: 21 }, (_, i) => {
        const major = i % 5 === 0;
        const a = point(cx, cy, r*(major ? .76 : .83), 135+i*13.5);
        const b = point(cx, cy, r*.91, 135+i*13.5);
        return <line key={i} x1={a[0]} y1={a[1]} x2={b[0]} y2={b[1]} stroke={metal} opacity={major ? 1 : .45} />;
      })}
      {(el.widget_style === "high_fantasy" ? [0,45,90,135,180,225,270,315] : [0,90,180,270]).map((angle) => {
        const p = point(cx, cy, r+2.5, angle);
        return el.widget_style === "cyberpunk" ? <rect key={angle} x={p[0]-1.5} y={p[1]-1.5} width={3} height={3} fill={rgb(el.color)} />
          : <polygon key={angle} points={diamond(...p,2.5)} fill={rgb(el.color)} stroke={metal} />;
      })}
      {(el.widget_style === "cyberpunk" ? [[10,"50"]] : [[0,"0"],[10,"50"],[20,"100"]]).map(([i,label]) => {
        const p = point(cx,cy,r*.64,135+i*13.5);
        return <text key={i} x={p[0]} y={p[1]} textAnchor="middle" dominantBaseline="middle"
          fontSize={Math.max(7.5,r*.13)} fill={rgb(el.muted_color)}>{label}</text>;
      })}
      <polygon points={diamond(cx,cy,3.5)} fill={rgb(el.color)} stroke={metal} />
      <text x={cx} y={cy+r*.46} textAnchor="middle" dominantBaseline="middle"
        fill={rgb(el.text_color)} fontSize={Math.max(10,r*.28)}>--</text>
      {el.show_title !== false && <text x={cx} y={cy-r-12} textAnchor="middle" dominantBaseline="middle"
        fill={rgb(el.text_color)} fontWeight={650} fontSize={Math.max(10,r*.18)}>{title}</text>}
    </g>
  );
}

export function StyledBoxPreview({ el, x, y, w, h, title }) {
  const metal = rgb(el.ornament_color);
  if (el.type === "bar") {
    const vertical = el.orientation === "vertical";
    const length = vertical ? h : w;
    const thickness = Math.min(14, vertical ? w : h);
    return <g opacity={el.opacity ?? 1} style={{ pointerEvents: "none", fontFamily: widgetFont(el) }}>
      <g transform={`translate(${x+w/2} ${y+h/2})${vertical ? " rotate(-90)" : ""}`}>
        <Meter x={-length/2} y={-thickness/2} w={length} h={thickness} el={el} />
      </g>
      {el.show_title !== false && <text x={x+w/2} y={y-22} textAnchor="middle" fill={rgb(el.text_color)} fontSize={11}>{title}</text>}
      {el.show_value !== false && <text x={x+w/2} y={y-3} textAnchor="middle" fill={rgb(el.text_color)} fontSize={13}>--</text>}
    </g>;
  }
  const art = el.show_art !== false;
  const name = el.show_name !== false;
  const progress = el.show_time !== false;
  const rows = (name ? 42 : 0) + (progress ? 32 : 0);
  const frameSpace = el.widget_style === "cyberpunk" ? 28 : 48;
  const a = art ? Math.max(0,Math.min(w-38,h-rows-frameSpace)) : 0;
  const top = y+Math.max(0,(h-(art && a > 0 ? a+frameSpace : 0)-rows)/2);
  const ax = x+(w-a)/2, ay = top+frameSpace-14;
  const textY = art ? ay+a+14 : top;
  return <g opacity={el.opacity ?? 1} style={{ pointerEvents: "none", fontFamily: widgetFont(el) }}>
    {art && a > 0 && <>
      {el.widget_style === "gothic" ? <path d={`M ${ax-6} ${ay+a+6} V ${ay+6} Q ${ax-6} ${top+12} ${x+w/2} ${top} Q ${ax+a+6} ${top+12} ${ax+a+6} ${ay+6} V ${ay+a+6}`}
        fill="none" stroke={metal} strokeWidth={2} />
        : el.widget_style === "cyberpunk" ? [1,-1].flatMap((dx) => [1,-1].map((dy) => {
          const px=dx === 1 ? ax-5 : ax+a+5, py=dy === 1 ? ay-5 : ay+a+5;
          return <path key={`${dx}${dy}`} d={`M ${px} ${py+dy*22} V ${py} H ${px+dx*22}`}
            fill="none" stroke={rgb(el.color)} strokeWidth={2} />;
        })) : <>
          <rect x={ax-5} y={ay-5} width={a+10} height={a+10} fill="none" stroke={metal} />
          <rect x={ax-8} y={ay-8} width={a+16} height={a+16} fill="none" stroke={metal} strokeOpacity={.4} />
          <polygon points={Array.from({length:8},(_,i)=>point(x+w/2,top+12,i%2 ? 4 : 10,-90+i*45).join(",")).join(" ")}
            fill={rgb(el.color)} stroke={metal} />
        </>}
      <rect x={ax} y={ay} width={a} height={a} fill={rgb(el.face_color)} stroke={metal} />
      {[.29,.25,.06].map((r) => <circle key={r} cx={x+w/2} cy={ay+a/2} r={a*r} fill="none" stroke={metal} />)}
      {el.widget_style === "gothic" && <polygon points={diamond(x+w/2,top+2,3)} fill={rgb(el.color)} stroke={metal} />}
    </>}
    {name && <>
      <text x={x+w/2} y={textY+14} textAnchor="middle" fill={rgb(el.text_color)} fontSize={16}>Spotify</text>
      <text x={x+w/2} y={textY+34} textAnchor="middle" fill={rgb(el.muted_color)} fontSize={12}>Track / Artist</text>
    </>}
    {progress && <Meter x={x+12} y={textY+(name ? 42 : 0)} w={w-24} h={7} el={{...el,id:`${el.id}_progress`}} />}
  </g>;
}
