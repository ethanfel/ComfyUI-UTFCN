import assert from "node:assert/strict";
import test from "node:test";

import { applyWidgetTransfers, planWidgetTransfers } from "../web/utfcn_widget_transfer.js";

test("missing nodes carry serialized widget values by widget slot", () => {
    const source = {
        widgets: [],
        last_serialization: {
            widgets_values: ["first prompt\nsecond prompt", "secondary text"],
        },
    };
    const target = {
        widgets: [
            { name: "text", value: "" },
            { name: "fallback", value: "" },
        ],
    };
    const shape = {
        widgets: target.widgets.map((w) => ({ name: w.name, type: w.type })),
        widgetNames: target.widgets.map((w) => w.name),
    };

    const result = planWidgetTransfers(source, shape, {}, { allowSerializedIndexFallback: true });

    assert.deepEqual(result.warns, []);
    assert.deepEqual(result.wMap, [
        { fromIndex: 0, toIndex: 0, to: "text", serialized: true },
        { fromIndex: 1, toIndex: 1, to: "fallback", serialized: true },
    ]);

    assert.equal(applyWidgetTransfers(source, target, result.wMap), 2);
    assert.equal(target.widgets[0].value, "first prompt\nsecond prompt");
    assert.equal(target.widgets[1].value, "secondary text");
});

test("live named widgets do not fall back to index unless explicitly allowed", () => {
    const source = {
        widgets: [{ name: "strength", value: 0.4 }],
        last_serialization: { widgets_values: [0.4] },
    };
    const shape = {
        widgets: [{ name: "text", type: "text" }],
        widgetNames: ["text"],
    };

    const result = planWidgetTransfers(source, shape, {}, { allowSerializedIndexFallback: false });

    assert.deepEqual(result.wMap, []);
    assert.deepEqual(result.warns, ["widget \"strength\" value not carried"]);
});

test("named widget plans can read serialized value when live value is absent", () => {
    const source = {
        widgets: [{ name: "text" }],
        last_serialization: { widgets_values: ["saved from workflow"] },
    };
    const target = { widgets: [{ name: "text", value: "" }] };
    const shape = {
        widgets: [{ name: "text", type: "text" }],
        widgetNames: ["text"],
    };

    const result = planWidgetTransfers(source, shape, {}, { allowSerializedIndexFallback: false });

    assert.deepEqual(result.wMap, [{ from: "text", fromIndex: 0, to: "text", toIndex: 0 }]);
    assert.equal(applyWidgetTransfers(source, target, result.wMap), 1);
    assert.equal(target.widgets[0].value, "saved from workflow");
});

test("serialized fallback suppresses live-name warnings when it carries the same slot", () => {
    const source = {
        widgets: [{ name: "custom_text", value: "placeholder text" }],
        last_serialization: { widgets_values: ["placeholder text"] },
    };
    const shape = {
        widgets: [{ name: "text", type: "text" }],
        widgetNames: ["text"],
    };

    const result = planWidgetTransfers(source, shape, {}, { allowSerializedIndexFallback: true });

    assert.deepEqual(result.warns, []);
    assert.deepEqual(result.wMap, [{ fromIndex: 0, toIndex: 0, to: "text", serialized: true }]);
});

test("serialized transfers prefer saved workflow values over live placeholder defaults", () => {
    const source = {
        widgets: [{ name: "custom_text", value: "" }],
        last_serialization: { widgets_values: ["preserved text"] },
    };
    const target = { widgets: [{ name: "text", value: "" }] };
    const shape = {
        widgets: [{ name: "text", type: "text" }],
        widgetNames: ["text"],
    };

    const result = planWidgetTransfers(source, shape, {}, { allowSerializedIndexFallback: true });

    assert.equal(applyWidgetTransfers(source, target, result.wMap), 1);
    assert.equal(target.widgets[0].value, "preserved text");
});
