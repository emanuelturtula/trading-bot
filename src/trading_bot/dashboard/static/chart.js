// A ticker's chart on the dashboard (spec 027).
//
// Reads `chart.json` (dashboard/chart.py) and draws it with TradingView Lightweight Charts, vendored
// in static/vendor/. Nothing here parses HTML: labels reach the page as text.
"use strict";

(function () {
  const node = document.getElementById("chart");
  const legend = document.getElementById("chart-legend");
  const library = window.LightweightCharts;
  if (!node || !legend) {
    return;
  }
  if (!library) {
    message("The chart library did not load.");
    return;
  }

  const UP = "#26a69a";
  const DOWN = "#ef5350";
  const PALETTE = ["#2962ff", "#f57c00", "#8e24aa", "#00897b", "#6d4c41", "#d81b60"];
  const style = getComputedStyle(document.documentElement);

  function cssColor(name, fallback) {
    return style.getPropertyValue(name).trim() || fallback;
  }

  function faded(hex) {
    const value = Number.parseInt(hex.slice(1), 16);
    return `rgba(${(value >> 16) & 255}, ${(value >> 8) & 255}, ${value & 255}, 0.5)`;
  }

  function message(text) {
    const paragraph = document.createElement("p");
    paragraph.className = "muted";
    paragraph.textContent = text;
    node.replaceChildren(paragraph);
  }

  function legendItem(text, color, symbol) {
    const item = document.createElement("span");
    if (symbol) {
      const mark = document.createElement("b");
      mark.style.color = color;
      mark.textContent = `${symbol} `;
      item.append(mark);
    } else {
      const swatch = document.createElement("i");
      swatch.style.background = color;
      item.append(swatch);
    }
    item.append(text);
    legend.append(item);
  }

  function marker(item) {
    const buy = item.side === "BUY";
    const color = buy ? UP : DOWN;
    if (item.recorded) {
      return {
        time: item.time,
        position: buy ? "belowBar" : "aboveBar",
        shape: buy ? "arrowUp" : "arrowDown",
        color,
        text: `${item.side} · ${item.rule}`,
      };
    }
    return {
      time: item.time,
      position: buy ? "belowBar" : "aboveBar",
      shape: "circle",
      color: faded(color),
      size: 0.6,
    };
  }

  function draw(data) {
    if (!data.candles.length) {
      message("There is no closed candle to show yet.");
      return;
    }
    node.replaceChildren();
    const muted = cssColor("--muted", "#5d6677");
    const line = cssColor("--line", "#dfe3ea");
    const chart = library.createChart(node, {
      autoSize: true,
      localization: { locale: "en-US" }, // the browser's tag may be one Intl rejects
      layout: {
        background: { color: "transparent" },
        textColor: muted,
        panes: { separatorColor: line },
      },
      grid: { vertLines: { color: line }, horzLines: { color: line } },
      rightPriceScale: { borderColor: line },
      timeScale: { borderColor: line, timeVisible: !["1d", "1w"].includes(data.timeframe), secondsVisible: false },
    });
    const candles = chart.addSeries(library.CandlestickSeries, {
      upColor: UP,
      downColor: DOWN,
      wickUpColor: UP,
      wickDownColor: DOWN,
      borderVisible: false,
    });
    candles.setData(data.candles);

    const firstOfPane = new Map([[0, candles]]);
    data.lines.forEach((item, index) => {
      const color = PALETTE[index % PALETTE.length];
      const series = chart.addSeries(
        library.LineSeries,
        { color, lineWidth: 2, priceLineVisible: false, lastValueVisible: false },
        item.pane,
      );
      series.setData(item.points);
      if (!firstOfPane.has(item.pane)) {
        firstOfPane.set(item.pane, series);
      }
      legendItem(item.label, color);
    });
    for (const level of data.levels) {
      const series = firstOfPane.get(level.pane);
      if (series) {
        series.createPriceLine({
          price: level.value,
          color: muted,
          lineWidth: 1,
          lineStyle: library.LineStyle.Dashed,
          axisLabelVisible: true,
          title: "",
        });
      }
    }
    library.createSeriesMarkers(candles, data.markers.map(marker));

    const panes = chart.panes();
    if (panes.length > 1) {
      panes[0].setStretchFactor(3);
      panes.slice(1).forEach((pane) => pane.setStretchFactor(1));
    }
    chart.timeScale().fitContent();
    legendItem("BUY sent", UP, "▲");
    legendItem("SELL sent", DOWN, "▼");
    legendItem("where an assigned rule would have fired", muted, "●");
  }

  fetch(node.dataset.src, { credentials: "same-origin", headers: { Accept: "application/json" } })
    .then(async (response) => {
      let body = null;
      try {
        body = await response.json();
      } catch (error) {
        body = null;
      }
      if (!response.ok || !body || body.error) {
        throw new Error(body && body.error ? body.error : "The chart could not be loaded.");
      }
      return body;
    })
    .then(draw)
    .catch((error) => message(error.message));
})();
