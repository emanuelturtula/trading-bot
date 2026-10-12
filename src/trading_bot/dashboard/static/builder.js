// Rule builder of the dashboard (spec 026).
//
// Progressive enhancement over the JSON editor: the form still submits the `document` textarea,
// which this script keeps in sync with the builder. Without JavaScript the JSON editor works as
// before. Templates are data from the server (`dashboard/builder.py`); `fill` and `unify` mirror the
// Python reference. Nothing here parses HTML: every value reaches the page as text.
"use strict";

(function () {
  const dataNode = document.getElementById("builder-data");
  const textarea = document.getElementById("document");
  const root = document.getElementById("builder");
  const jsonBlock = document.getElementById("json-editor");
  if (!dataNode || !textarea || !root || !jsonBlock) {
    return;
  }
  const data = JSON.parse(dataNode.textContent);
  const templates = new Map(data.templates.map((template) => [template.id, template]));
  const catalog = new Map(data.catalog.map((indicator) => [indicator.name, indicator]));
  const CUSTOM = "custom";
  const OPERATOR_LABELS = {
    "<": "is below",
    "<=": "is at or below",
    ">": "is above",
    ">=": "is at or above",
    crosses_above: "crosses above",
    crosses_below: "crosses below",
  };
  const MODE_LABELS = { all: "all of these hold", any: "any of these holds" };
  const OTHER_MODE = { all: "any", any: "all" };

  let state = null;

  // --- DOM helpers ---------------------------------------------------------------------------

  function el(tag, attributes, children) {
    const node = document.createElement(tag);
    for (const [key, value] of Object.entries(attributes || {})) {
      if (value === false || value === null || value === undefined) {
        continue;
      }
      if (key === "text") {
        node.textContent = value;
      } else {
        node.setAttribute(key, value === true ? "" : String(value));
      }
    }
    for (const child of children || []) {
      if (child) {
        node.append(child);
      }
    }
    return node;
  }

  function select(options, current, onChange, label) {
    const node = el("select", { "aria-label": label });
    for (const [value, text] of options) {
      node.append(el("option", { value, selected: value === current, text }));
    }
    node.addEventListener("change", () => onChange(node.value));
    return node;
  }

  function numberInput(value, onChange, label, limits) {
    const node = el("input", {
      type: "number",
      value,
      "aria-label": label,
      min: limits && limits.min,
      max: limits && limits.max,
      step: limits && limits.kind === "int" ? 1 : "any",
    });
    node.addEventListener("input", () => onChange(readNumber(node.value, limits && limits.kind)));
    return node;
  }

  function readNumber(text, kind) {
    const number = kind === "int" ? Number.parseInt(text, 10) : Number.parseFloat(text);
    return Number.isFinite(number) ? number : text; // the server reports a non-number
  }

  function labeled(text, control) {
    return el("label", { class: "builder-field" }, [el("span", { text }), control]);
  }

  // --- templates -----------------------------------------------------------------------------

  function isSlot(pattern) {
    return (
      pattern !== null &&
      typeof pattern === "object" &&
      !Array.isArray(pattern) &&
      Object.keys(pattern).length === 1 &&
      "$" in pattern
    );
  }

  function fill(pattern, values) {
    if (isSlot(pattern)) {
      return values[pattern.$];
    }
    if (Array.isArray(pattern)) {
      return pattern.map((item) => fill(item, values));
    }
    if (pattern !== null && typeof pattern === "object") {
      return Object.fromEntries(Object.entries(pattern).map(([k, v]) => [k, fill(v, values)]));
    }
    return pattern;
  }

  function unify(pattern, value, bound) {
    if (isSlot(pattern)) {
      if (pattern.$ in bound) {
        return JSON.stringify(bound[pattern.$]) === JSON.stringify(value);
      }
      bound[pattern.$] = value;
      return true;
    }
    if (Array.isArray(pattern)) {
      return (
        Array.isArray(value) &&
        value.length === pattern.length &&
        pattern.every((item, index) => unify(item, value[index], bound))
      );
    }
    if (pattern !== null && typeof pattern === "object") {
      if (value === null || typeof value !== "object" || Array.isArray(value)) {
        return false;
      }
      const keys = Object.keys(pattern);
      return (
        keys.length === Object.keys(value).length &&
        keys.every((key) => key in value && unify(pattern[key], value[key], bound))
      );
    }
    return pattern === value;
  }

  function accepts(field, value) {
    if (field.kind === "choice") {
      return field.choices.some(([choice]) => choice === value);
    }
    if (typeof value !== "number" || (field.kind === "int" && !Number.isInteger(value))) {
      return false;
    }
    return value >= field.min && value <= field.max;
  }

  function matchTemplate(conditions) {
    for (const template of data.templates) {
      const bound = {};
      if (
        unify(template.conditions, conditions, bound) &&
        template.fields.every((field) => accepts(field, bound[field.name]))
      ) {
        return { template: template.id, values: bound };
      }
    }
    return null;
  }

  function defaults(template) {
    return Object.fromEntries(template.fields.map((field) => [field.name, field.default]));
  }

  // --- custom conditions ---------------------------------------------------------------------

  function isOperand(operand) {
    if (operand === null || typeof operand !== "object" || Array.isArray(operand)) {
      return false;
    }
    const keys = Object.keys(operand);
    if ("indicator" in operand) {
      const indicator = catalog.get(operand.indicator);
      return (
        indicator !== undefined &&
        keys.every((key) => ["indicator", "params", "output"].includes(key)) &&
        (operand.params === undefined ||
          (operand.params !== null &&
            typeof operand.params === "object" &&
            Object.keys(operand.params).every((name) =>
              indicator.params.some((param) => param.name === name),
            ))) &&
        (operand.output === undefined ||
          indicator.outputs.some((output) => output.name === operand.output))
      );
    }
    if ("price" in operand) {
      return keys.length === 1 && data.prices.includes(operand.price);
    }
    return keys.length === 1 && "value" in operand;
  }

  function isCondition(item) {
    return (
      item !== null &&
      typeof item === "object" &&
      Object.keys(item).length === 3 &&
      isOperand(item.left) &&
      isOperand(item.right) &&
      item.op in OPERATOR_LABELS
    );
  }

  function groupMode(group) {
    if (group === null || typeof group !== "object" || Array.isArray(group)) {
      return null;
    }
    const keys = Object.keys(group);
    return keys.length === 1 && (keys[0] === "all" || keys[0] === "any") ? keys[0] : null;
  }

  function isCustom(conditions) {
    const mode = groupMode(conditions);
    if (!mode || !Array.isArray(conditions[mode])) {
      return false;
    }
    return conditions[mode].every((item) => {
      if (isCondition(item)) {
        return true;
      }
      const nested = groupMode(item);
      return (
        nested === OTHER_MODE[mode] &&
        Array.isArray(item[nested]) &&
        item[nested].every(isCondition)
      );
    });
  }

  function indicatorOperand(name) {
    const indicator = catalog.get(name);
    return {
      indicator: name,
      params: Object.fromEntries(indicator.params.map((param) => [param.name, param.default])),
      output: indicator.default_output || indicator.outputs[0].name,
    };
  }

  function newOperand(kind) {
    if (kind === "indicator") {
      return indicatorOperand("rsi");
    }
    return kind === "price" ? { price: "close" } : { value: 30 };
  }

  function newCondition() {
    return { left: indicatorOperand("rsi"), op: "<", right: { value: 30 } };
  }

  function operandKind(operand) {
    return "indicator" in operand ? "indicator" : "price" in operand ? "price" : "value";
  }

  // --- document <-> state --------------------------------------------------------------------

  const COMMON_KEYS = ["name", "signal", "timeframe", "conditions", "cooldown_bars"];

  function stateOf(documentValue) {
    if (
      documentValue === null ||
      typeof documentValue !== "object" ||
      Array.isArray(documentValue) ||
      !Object.keys(documentValue).every((key) => COMMON_KEYS.includes(key))
    ) {
      return null;
    }
    const common = {
      name: typeof documentValue.name === "string" ? documentValue.name : "",
      signal: documentValue.signal === "SELL" ? "SELL" : "BUY",
      timeframe: data.timeframes.includes(documentValue.timeframe)
        ? documentValue.timeframe
        : data.timeframes[0],
      cooldown_bars: "cooldown_bars" in documentValue ? documentValue.cooldown_bars : 0,
    };
    const matched = matchTemplate(documentValue.conditions);
    if (matched) {
      return { common, kind: matched.template, values: matched.values, custom: null };
    }
    if (isCustom(documentValue.conditions)) {
      return { common, kind: CUSTOM, values: null, custom: documentValue.conditions };
    }
    return null;
  }

  function conditions() {
    if (state.kind === CUSTOM) {
      return state.custom;
    }
    return fill(templates.get(state.kind).conditions, state.values);
  }

  function sync() {
    const documentValue = {
      name: state.common.name,
      signal: state.common.signal,
      timeframe: state.common.timeframe,
      conditions: conditions(),
      cooldown_bars: state.common.cooldown_bars,
    };
    textarea.value = JSON.stringify(documentValue, null, 2);
  }

  // --- rendering -----------------------------------------------------------------------------

  function render() {
    root.replaceChildren(
      modeBar(true),
      commonFields(),
      el("div", { class: "builder-field" }, [
        el("span", { text: "Kind" }),
        select(
          [
            ...data.templates.map((template) => [template.id, template.label]),
            [CUSTOM, "Custom (build the conditions)"],
          ],
          state.kind,
          changeKind,
          "Kind",
        ),
      ]),
      state.kind === CUSTOM ? customEditor() : templateEditor(templates.get(state.kind)),
    );
    sync();
  }

  function modeBar(inBuilder) {
    const button = el("button", {
      type: "button",
      class: "link-button",
      text: inBuilder ? "Edit as JSON" : "Back to the builder",
    });
    button.addEventListener("click", inBuilder ? showJson : showBuilder);
    return el("p", { class: "builder-mode" }, [button]);
  }

  function commonFields() {
    const name = el("input", { type: "text", value: state.common.name, "aria-label": "Name" });
    name.addEventListener("input", () => {
      state.common.name = name.value;
      sync();
    });
    return el("div", { class: "builder-grid" }, [
      labeled("Name", name),
      labeled(
        "Signal",
        select(
          [
            ["BUY", "BUY"],
            ["SELL", "SELL"],
          ],
          state.common.signal,
          (value) => {
            state.common.signal = value;
            sync();
          },
          "Signal",
        ),
      ),
      labeled(
        "Timeframe",
        select(
          data.timeframes.map((timeframe) => [timeframe, timeframe]),
          state.common.timeframe,
          (value) => {
            state.common.timeframe = value;
            sync();
          },
          "Timeframe",
        ),
      ),
      labeled(
        "Cooldown (candles)",
        numberInput(
          state.common.cooldown_bars,
          (value) => {
            state.common.cooldown_bars = value;
            sync();
          },
          "Cooldown (candles)",
          { kind: "int", min: 0 },
        ),
      ),
    ]);
  }

  function changeKind(kind) {
    if (kind === CUSTOM) {
      state.custom = JSON.parse(JSON.stringify(conditions()));
    } else {
      state.values = defaults(templates.get(kind));
    }
    state.kind = kind;
    render();
  }

  function templateEditor(template) {
    const fields = template.fields.map((field) => {
      const update = (value) => {
        state.values[field.name] = value;
        sync();
      };
      const control =
        field.kind === "choice"
          ? select(field.choices, state.values[field.name], update, field.label)
          : numberInput(state.values[field.name], update, field.label, field);
      return labeled(field.label, control);
    });
    return el("div", {}, [
      el("p", { class: "muted", text: template.description }),
      el("div", { class: "builder-grid" }, fields),
    ]);
  }

  function customEditor() {
    return el("div", { class: "builder-custom" }, [groupEditor(state.custom, null)]);
  }

  function groupEditor(group, parent) {
    const mode = groupMode(group);
    const items = group[mode];
    const heading = parent
      ? el("p", { class: "builder-group-mode", text: `Group: ${MODE_LABELS[mode]}` })
      : el("div", { class: "builder-field" }, [
          el("span", { text: "Fires when" }),
          select(
            [
              ["all", MODE_LABELS.all],
              ["any", MODE_LABELS.any],
            ],
            mode,
            (value) => {
              state.custom = flip(state.custom, value);
              render();
            },
            "Fires when",
          ),
        ]);
    const rows = items.map((item, index) => {
      const remove = () => {
        items.splice(index, 1);
        render();
      };
      if (isCondition(item)) {
        return conditionEditor(item, remove);
      }
      return el("div", { class: "builder-group" }, [
        groupEditor(item, group),
        actionButton("Remove group", remove),
      ]);
    });
    const actions = [
      actionButton("Add condition", () => {
        items.push(newCondition());
        render();
      }),
    ];
    if (!parent) {
      actions.push(
        actionButton("Add group", () => {
          items.push({ [OTHER_MODE[mode]]: [newCondition(), newCondition()] });
          render();
        }),
      );
    }
    return el("div", {}, [heading, ...rows, el("p", { class: "row" }, actions)]);
  }

  function flip(group, mode) {
    const current = groupMode(group);
    if (current === mode) {
      return group;
    }
    const items = group[current].map((item) => {
      const nested = groupMode(item);
      return nested ? { [OTHER_MODE[mode]]: item[nested] } : item;
    });
    return { [mode]: items };
  }

  function actionButton(text, onClick) {
    const button = el("button", { type: "button", class: "small", text });
    button.addEventListener("click", onClick);
    return button;
  }

  function conditionEditor(condition, remove) {
    const row = el("div", { class: "builder-condition" });
    const draw = () => {
      row.replaceChildren(
        operandEditor(condition, "left", draw),
        select(
          Object.entries(OPERATOR_LABELS),
          condition.op,
          (value) => {
            condition.op = value;
            sync();
          },
          "Operator",
        ),
        operandEditor(condition, "right", draw),
        actionButton("Remove", remove),
      );
      sync();
    };
    draw();
    return row;
  }

  function operandEditor(condition, side, redraw) {
    const operand = condition[side];
    const kind = operandKind(operand);
    const parts = [
      select(
        [
          ["indicator", "Indicator"],
          ["price", "Candle"],
          ["value", "Number"],
        ],
        kind,
        (value) => {
          condition[side] = newOperand(value);
          redraw();
        },
        `${side} operand`,
      ),
    ];
    if (kind === "indicator") {
      const indicator = catalog.get(operand.indicator);
      parts.push(
        select(
          data.catalog.map((item) => [item.name, item.label]),
          operand.indicator,
          (value) => {
            condition[side] = indicatorOperand(value);
            redraw();
          },
          `${side} indicator`,
        ),
      );
      operand.params = operand.params || {};
      for (const param of indicator.params) {
        const value = param.name in operand.params ? operand.params[param.name] : param.default;
        parts.push(
          labeled(
            param.label,
            numberInput(
              value,
              (number) => {
                operand.params[param.name] = number;
                sync();
              },
              param.label,
              { kind: param.type, min: param.min, max: param.max },
            ),
          ),
        );
      }
      if (indicator.outputs.length > 1) {
        parts.push(
          select(
            indicator.outputs.map((output) => [output.name, output.label]),
            operand.output || indicator.default_output || indicator.outputs[0].name,
            (value) => {
              operand.output = value;
              sync();
            },
            `${side} output`,
          ),
        );
      }
    } else if (kind === "price") {
      parts.push(
        select(
          data.prices.map((price) => [price, price]),
          operand.price,
          (value) => {
            operand.price = value;
            sync();
          },
          `${side} candle column`,
        ),
      );
    } else {
      parts.push(
        numberInput(
          operand.value,
          (number) => {
            operand.value = number;
            sync();
          },
          `${side} number`,
          { kind: "float" },
        ),
      );
    }
    return el("span", { class: "builder-operand" }, parts);
  }

  // --- modes ---------------------------------------------------------------------------------

  const jsonBar = modeBar(false);
  const notice = el("p", { class: "flash error", hidden: true });
  jsonBlock.prepend(jsonBar, notice);

  function showJson() {
    root.hidden = true;
    jsonBlock.hidden = false;
    notice.hidden = true;
  }

  function showBuilder() {
    let parsed = null;
    try {
      parsed = stateOf(JSON.parse(textarea.value));
    } catch (error) {
      parsed = null;
    }
    if (!parsed) {
      notice.textContent =
        "The builder cannot show this document. Fix it or keep editing it as JSON.";
      notice.hidden = false;
      return;
    }
    state = parsed;
    root.hidden = false;
    jsonBlock.hidden = true;
    notice.hidden = true;
    render();
  }

  // Open on the builder when the document fits it; otherwise stay in the JSON editor.
  showBuilder();
  notice.hidden = true;
})();
