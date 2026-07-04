/** A widget the user converted into an input slot -- its value lives on the input, not the widget. */
export const isConvertedWidget = (w) => w?.type === "converted-widget" || w?.type === "hidden";

const hasStoredValue = (v) => v !== undefined;
const hasReportableValue = (v) => v !== undefined && v !== null && v !== "";

function shapeWidgets(shape) {
    if (Array.isArray(shape?.widgets)) return shape.widgets;
    return (shape?.widgetNames || []).map((name) => ({ name }));
}

export function serializedWidgetValues(node) {
    const values = node?.last_serialization?.widgets_values;
    return Array.isArray(values) ? values : [];
}

function findTargetWidgetIndex(widgets, name, used) {
    return widgets.findIndex((w, i) => !used.has(i) && !isConvertedWidget(w) && w.name === name);
}

/**
 * Plan widget value transfers.
 *
 * Installed nodes can match by widget name (plus curated widget remaps). Missing
 * nodes often have no live widget objects, only ComfyUI's ordered
 * last_serialization.widgets_values array, so they may also opt into same-index
 * fallback.
 */
export function planWidgetTransfers(node, shape, rule = {}, options = {}) {
    const targetWidgets = shapeWidgets(shape);
    const liveWidgets = Array.isArray(node?.widgets) ? node.widgets : [];
    const serializedValues = serializedWidgetValues(node);
    const allowSerializedIndexFallback = !!options.allowSerializedIndexFallback;

    const usedTargets = new Set();
    const mappedSources = new Set();
    const pendingWarns = [];
    const pendingWarnSources = new Set();
    const wMap = [];
    const warns = [];

    liveWidgets.forEach((w, sourceIndex) => {
        if (w?.name == null || isConvertedWidget(w)) return;
        const want = rule?.widgets?.[w.name] ?? w.name;
        const targetIndex = findTargetWidgetIndex(targetWidgets, want, usedTargets);
        if (targetIndex >= 0) {
            usedTargets.add(targetIndex);
            mappedSources.add(sourceIndex);
            wMap.push({ from: w.name, fromIndex: sourceIndex, to: targetWidgets[targetIndex].name, toIndex: targetIndex });
        } else if (hasReportableValue(w.value)) {
            pendingWarns.push({ sourceIndex, message: `widget "${w.name}" value not carried` });
            pendingWarnSources.add(sourceIndex);
        }
    });

    if (allowSerializedIndexFallback) {
        serializedValues.forEach((value, sourceIndex) => {
            if (!hasStoredValue(value) || mappedSources.has(sourceIndex)) return;
            const target = targetWidgets[sourceIndex];
            if (target && !usedTargets.has(sourceIndex) && !isConvertedWidget(target) && target.name != null) {
                usedTargets.add(sourceIndex);
                mappedSources.add(sourceIndex);
                wMap.push({ fromIndex: sourceIndex, toIndex: sourceIndex, to: target.name, serialized: true });
            } else if (hasReportableValue(value) && !pendingWarnSources.has(sourceIndex)) {
                warns.push(`serialized widget #${sourceIndex + 1} value not carried`);
            }
        });
    }

    pendingWarns.forEach((warn) => {
        if (!mappedSources.has(warn.sourceIndex)) warns.push(warn.message);
    });

    return { wMap, warns };
}

function widgetByName(widgets, name) {
    return widgets.find((w) => w?.name === name);
}

function sourceWidgetValue(node, transfer) {
    const widgets = Array.isArray(node?.widgets) ? node.widgets : [];
    const values = serializedWidgetValues(node);

    if (transfer.serialized && Number.isInteger(transfer.fromIndex)) {
        if (transfer.fromIndex < values.length && hasStoredValue(values[transfer.fromIndex])) return values[transfer.fromIndex];
    }
    if (transfer.from != null) {
        const widget = widgetByName(widgets, transfer.from);
        if (hasStoredValue(widget?.value)) return widget.value;
    }
    if (Number.isInteger(transfer.fromIndex)) {
        const widget = widgets[transfer.fromIndex];
        if (hasStoredValue(widget?.value)) return widget.value;
        if (transfer.fromIndex < values.length && hasStoredValue(values[transfer.fromIndex])) return values[transfer.fromIndex];
    }
    return undefined;
}

function targetWidget(node, transfer) {
    const widgets = Array.isArray(node?.widgets) ? node.widgets : [];
    if (transfer.to != null) {
        const widget = widgetByName(widgets, transfer.to);
        if (widget) return widget;
    }
    if (Number.isInteger(transfer.toIndex)) return widgets[transfer.toIndex];
    return null;
}

export function applyWidgetTransfers(source, target, transfers) {
    let applied = 0;
    (transfers || []).forEach((transfer) => {
        const value = sourceWidgetValue(source, transfer);
        const widget = targetWidget(target, transfer);
        if (!widget || !hasStoredValue(value)) return;
        widget.value = value;
        try { widget.callback?.(widget.value); } catch {}
        applied++;
    });
    return applied;
}
