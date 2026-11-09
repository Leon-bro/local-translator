import { useEffect, useRef } from "react";

// Цвета дублируют токены из styles.css: canvas не читает CSS-переменные напрямую.
const COLORS: Record<string, string> = {
  p: "#c5ced8",
  a: "#6f84a0",
  b: "#2742c4",
  f: "#ffc933",
  x: "#ffc933",
  e: "#c8242f",
};

interface Props {
  strip: string;
  height: number;
  onPick?: (index: number) => void;
  label: string;
}

/** Лента книги: одна метка на кусок текста. На canvas, потому что в книге их тысячи. */
export function Ribbon({ strip, height, onPick, label }: Props) {
  const canvas = useRef<HTMLCanvasElement>(null);

  useEffect(() => {
    const element = canvas.current;
    if (!element) return;
    const draw = () => {
      const ratio = window.devicePixelRatio || 1;
      const width = element.clientWidth;
      if (!width) return;
      element.width = width * ratio;
      element.height = height * ratio;
      const ctx = element.getContext("2d")!;
      ctx.scale(ratio, ratio);
      ctx.clearRect(0, 0, width, height);
      const count = strip.length;
      if (!count) {
        ctx.fillStyle = COLORS.p;
        ctx.fillRect(0, 0, width, height);
        return;
      }
      // Метки делят ширину поровну; при нехватке места помеченные рисуются поверх остальных и не теряются.
      const step = width / count;
      const gap = step >= 5 ? 2 : 0;
      for (let pass = 0; pass < 2; pass++) {
        for (let i = 0; i < count; i++) {
          const state = strip[i];
          const important = state === "f" || state === "x" || state === "e";
          if ((pass === 1) !== important) continue;
          ctx.fillStyle = COLORS[state] ?? COLORS.p;
          const x = Math.floor(i * step);
          const w = Math.max(1, Math.ceil(step) - gap);
          ctx.fillRect(x, 0, important ? Math.max(w, 2) : w, height);
        }
      }
    };
    draw();
    const observer = new ResizeObserver(draw);
    observer.observe(element);
    return () => observer.disconnect();
  }, [strip, height]);

  const pick = (event: React.MouseEvent<HTMLCanvasElement>) => {
    if (!onPick || !strip.length) return;
    const box = event.currentTarget.getBoundingClientRect();
    const index = Math.floor(((event.clientX - box.left) / box.width) * strip.length);
    onPick(Math.min(strip.length - 1, Math.max(0, index)));
  };

  return (
    <canvas
      ref={canvas}
      className={onPick ? "ribbon ribbon-live" : "ribbon"}
      style={{ height }}
      role="img"
      aria-label={label}
      onClick={pick}
    />
  );
}
