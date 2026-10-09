"use client";

import { useEffect, useRef } from "react";
import {
  CandlestickSeries, ColorType, createChart, HistogramSeries, IChartApi, LineSeries, UTCTimestamp,
} from "lightweight-charts";
import { Bin } from "@/lib/api";
import { price as fmtPrice, usd } from "@/lib/format";

const THEME = {
  layout: { background: { type: ColorType.Solid, color: "#121722" }, textColor: "#8a94a8", fontSize: 11 },
  grid: { vertLines: { color: "#1b2230" }, horzLines: { color: "#1b2230" } },
  timeScale: { timeVisible: true, secondsVisible: false, borderColor: "#232b3a" },
  rightPriceScale: { borderColor: "#232b3a" },
};

export interface Candle { ts: string; open: number; high: number; low: number; close: number; volume: number; fees: number }

const t = (iso: string) => Math.floor(new Date(iso).getTime() / 1000) as UTCTimestamp;

/** Candles with volume and LP-fee bars in their own panes. */
export function CandleChart({ data }: { data: Candle[] }) {
  const ref = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (!ref.current) return;
    const chart: IChartApi = createChart(ref.current, { ...THEME, autoSize: true,
      localization: { priceFormatter: (p: number) => fmtPrice(p) } });
    const candles = chart.addSeries(CandlestickSeries, { upColor: "#3ecf8e", downColor: "#f2645a",
      wickUpColor: "#3ecf8e", wickDownColor: "#f2645a", borderVisible: false });
    candles.setData(data.filter((d) => d.open !== null).map((d) => ({ time: t(d.ts), open: d.open, high: d.high,
      low: d.low, close: d.close })));
    const vol = chart.addSeries(HistogramSeries, { color: "#7aa2ff88", priceFormat: { type: "volume" },
      title: "volume $" }, 1);
    vol.setData(data.map((d) => ({ time: t(d.ts), value: d.volume ?? 0 })));
    const fees = chart.addSeries(HistogramSeries, { color: "#e3b341aa", priceFormat: { type: "price", precision: 2,
      minMove: 0.01 }, title: "LP fees $" }, 2);
    fees.setData(data.map((d) => ({ time: t(d.ts), value: d.fees ?? 0 })));
    const panes = chart.panes();
    panes[0]?.setHeight(260);
    panes[1]?.setHeight(80);
    panes[2]?.setHeight(80);
    chart.timeScale().fitContent();
    return () => chart.remove();
  }, [data]);
  return <div ref={ref} className="h-[440px] w-full" />;
}

/** Fee/TVL (1 h) over time. */
export function FeeTvlChart({ data }: { data: { ts: string; fee_tvl_1h: number | null; tvl: number | null }[] }) {
  const ref = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (!ref.current) return;
    const chart = createChart(ref.current, { ...THEME, autoSize: true });
    const line = chart.addSeries(LineSeries, { color: "#e3b341", lineWidth: 2, title: "fee/TVL 1h %",
      priceFormat: { type: "price", precision: 3, minMove: 0.001 } });
    line.setData(data.filter((d) => d.fee_tvl_1h !== null)
      .map((d) => ({ time: t(d.ts), value: (d.fee_tvl_1h as number) * 100 })));
    const tvl = chart.addSeries(LineSeries, { color: "#7aa2ff", lineWidth: 1, title: "TVL $",
      priceFormat: { type: "volume" } }, 1);
    tvl.setData(data.filter((d) => d.tvl !== null).map((d) => ({ time: t(d.ts), value: d.tvl as number })));
    chart.panes()[1]?.setHeight(70);
    chart.timeScale().fitContent();
    return () => chart.remove();
  }, [data]);
  return <div ref={ref} className="h-[240px] w-full" />;
}

/**
 * Liquidity per bin around the active bin, valued in USD. Bins below the price hold the quote side, above hold the
 * base side (DLMM). `usdX`/`usdY`: USD price of token X / Y. Optional range overlay [min, max].
 */
export function BinsChart({ bins, activeBin, usdX, usdY, range }: {
  bins: Bin[]; activeBin: number; usdX: number | null; usdY: number | null; range?: { min: number; max: number } | null;
}) {
  if (!bins.length) return <p className="p-4 text-sm text-mut">No bin data yet.</p>;
  const values = bins.map((b) => (b.x || 0) * (usdX ?? 0) + (b.y || 0) * (usdY ?? 0));
  const max = Math.max(...values, 1);
  const total = values.reduce((a, b) => a + b, 0);
  const W = 1000;
  const H = 220;
  const bw = W / bins.length;
  return (
    <div>
      <svg viewBox={`0 0 ${W} ${H + 24}`} className="h-[260px] w-full" preserveAspectRatio="none">
        {range && (
          <rect x={bins.findIndex((b) => b.bin_id >= range.min) * bw} y={0}
            width={Math.max(bw, (range.max - range.min + 1) * bw)} height={H} fill="#7aa2ff" opacity={0.08} />
        )}
        {bins.map((b, i) => {
          const h = (values[i] / max) * (H - 10);
          const color = b.bin_id === activeBin ? "#e3b341" : (b.x > 0 && b.y === 0 ? "#7aa2ff" : "#3ecf8e");
          return (
            <rect key={b.bin_id} x={i * bw + 0.5} y={H - h} width={Math.max(bw - 1, 0.5)} height={h} fill={color}>
              <title>{`bin ${b.bin_id} · price ${fmtPrice(b.price)} · ${usd(values[i])} (X ${b.x.toFixed(2)}, Y ${b.y.toFixed(4)})`}</title>
            </rect>
          );
        })}
        <text x={4} y={H + 16} fill="#8a94a8" fontSize={14}>{fmtPrice(bins[0].price)}</text>
        <text x={W - 4} y={H + 16} fill="#8a94a8" fontSize={14} textAnchor="end">{fmtPrice(bins[bins.length - 1].price)}</text>
      </svg>
      <div className="flex flex-wrap gap-4 px-1 text-xs text-mut">
        <span><span className="inline-block h-2 w-2 bg-[#3ecf8e]" /> quote side (below price)</span>
        <span><span className="inline-block h-2 w-2 bg-[#7aa2ff]" /> base side (above price)</span>
        <span><span className="inline-block h-2 w-2 bg-[#e3b341]" /> active bin</span>
        <span className="ml-auto num">{bins.length} bins · {usd(total)} shown</span>
      </div>
    </div>
  );
}
