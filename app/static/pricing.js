(() => {
  const root = document.querySelector("#pricing-workbench");
  if (!root) return;

  const field = (id) => document.querySelector(`#${id}`);
  const quoteMessage = field("quoteMessage");
  const FIXED_SHIPPING_FEE = 0.4;
  let quoteTimer;
  let previewTimer;
  let quoteSequence = 0;
  let previewSequence = 0;
  async function api(path, options = {}) {
    const response = await fetch(path, {
      credentials: "include",
      headers: { "Content-Type": "application/json", ...(options.headers || {}) },
      ...options,
    });
    const data = await response.json().catch(() => ({}));
    if (!response.ok) {
      const detail = data.detail;
      const message = Array.isArray(detail) ? detail.map((item) => item.msg || item).join("；")
        : typeof detail === "string" ? detail : `请求失败（${response.status}）`;
      throw new Error(message);
    }
    return data;
  }

  const number = (id) => {
    const raw = field(id).value.trim();
    if (!raw) return null;
    const value = Number(raw);
    return Number.isFinite(value) ? value : null;
  };
  const optional = (id) => {
    const value = number(id);
    return value === null ? undefined : value;
  };
  const money = (value) => value === null || value === undefined || value === "" || !Number.isFinite(Number(value))
    ? "—" : `¥${Number(value).toFixed(2)}`;
  const escapeHtml = (value) => String(value ?? "").replace(/[&<>"']/g, (character) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[character]));

  function renderProfit(output, value, sign) {
    if (value === null || value === undefined || value === "" || !Number.isFinite(Number(value))) {
      output.textContent = "—";
      output.className = "pricing-output profit";
      return;
    }
    const amount = Number(value);
    if (sign === "negative" || amount < 0) {
      output.textContent = `−¥${Math.abs(amount).toFixed(2)}`;
      output.className = "pricing-output profit negative";
    } else if (sign === "positive" || amount > 0) {
      output.textContent = `+¥${amount.toFixed(2)}`;
      output.className = "pricing-output profit positive";
    } else {
      output.textContent = "¥0.00";
      output.className = "pricing-output profit zero";
    }
  }
  function readPayload() {
    const data = {
      product_type: field("productType").value,
      final_price: optional("finalPrice"),
    };
    if (data.product_type === "ordinary") {
      data.purchase_price = optional("purchasePrice");
      const packagingShippingTotal = optional("packagingFee");
      if (packagingShippingTotal !== undefined) {
        // The UI edits the combined amount. The API keeps its WPS-compatible
        // packaging_fee component and adds fixed L4 freight separately.
        data.packaging_fee = Number((packagingShippingTotal - FIXED_SHIPPING_FEE).toFixed(2));
      }
      data.multiplier = optional("multiplier");
    } else {
      data.material = field("material").value;
      data.filament_grams = optional("filamentGrams");
      data.print_hours = optional("printHours");
      data.print_minutes = optional("printMinutes");
    }
    return data;
  }
  async function updateQuote() {
    const sequence = ++quoteSequence;
    const data = readPayload();
    quoteMessage.textContent = "正在核价…";
    try {
      const result = await api("/api/pricing/quote", { method: "POST", body: JSON.stringify(data) });
      if (sequence !== quoteSequence) return;
      if (result.status !== "QUOTED") {
        field("formulaPrice").textContent = "—";
        renderProfit(field("formulaProfit"), null, null);
        renderProfit(field("manualProfit"), null, null);
        field("costPriceNote").textContent = "";
        quoteMessage.textContent = (result.missing || []).join("；") || "请补充核价所需信息。";
        return;
      }
      field("formulaPrice").textContent = money(result.formula_price);
      renderProfit(field("formulaProfit"), result.formula_profit, result.formula_profit_sign);
      renderProfit(field("manualProfit"), result.manual_profit, result.manual_profit_sign);
      const packagingFee = Number(result.packaging_fee);
      const shippingFee = Number(result.shipping_fee);
      const packagingShippingTotal = packagingFee + shippingFee;
      field("costPriceNote").textContent = result.product_type === "printed_3d"
        ? `成本价 ${money(result.cost_price)} = 打印成本 ${money(result.production_cost)} + 包装运费 ${money(result.packaging_shipping)} · 规则版本 ${result.rule_version}`
        : `核价成本 ${money(result.total_cost)}（供货成本 ${money(result.purchase_price)} + 包装运费合计 ${money(packagingShippingTotal)}：包装 ${money(packagingFee)} + 物流 ${money(shippingFee)}）· 规则版本 ${result.rule_version}`;
      quoteMessage.textContent = "核价已更新。人工定价独立填写，不会改变公式定价。";
    } catch (error) {
      if (sequence !== quoteSequence) return;
      quoteMessage.textContent = `核价失败：${error.message}`;
      field("formulaPrice").textContent = "—";
      renderProfit(field("formulaProfit"), null, null);
      renderProfit(field("manualProfit"), null, null);
    }
  }
  function scheduleQuote() {
    clearTimeout(quoteTimer);
    quoteTimer = setTimeout(updateQuote, 220);
  }
  function schedulePreview() {
    clearTimeout(previewTimer);
    previewTimer = setTimeout(updatePreview, 220);
  }

  async function updatePreview() {
    const sequence = ++previewSequence;
    const values = {
      product_weight_g: number("productWeight"),
      length_cm: number("productLength"),
      width_cm: number("productWidth"),
      height_cm: number("productHeight"),
    };
    if (Object.values(values).some((value) => value === null || value <= 0)) {
      field("predictedWeight").textContent = "—";
      field("predictedCartonDimensions").textContent = "—";
      field("predictedCartonName").textContent = "填写产品重量及尺寸";
      field("weightCalculation").textContent = "";
      return;
    }
    field("predictedCartonName").textContent = "正在匹配纸箱…";
    try {
      const result = await api("/api/pricing/packaging-preview", { method: "POST", body: JSON.stringify(values) });
      if (sequence !== previewSequence) return;
      if (result.status !== "PREDICTED" || !result.carton) {
        field("predictedWeight").textContent = "—";
        field("predictedCartonDimensions").textContent = "—";
        field("predictedCartonName").textContent = "当前纸箱目录中没有适配尺寸";
        field("weightCalculation").textContent = "";
        return;
      }
      const carton = result.carton;
      field("predictedWeight").textContent = `${Number(result.predicted_weight_g).toFixed(2)} g`;
      field("weightCalculation").textContent = `${Number(values.product_weight_g).toFixed(2)} g 产品 + ${carton.recommended_weight_g} g 包装`;
      field("predictedCartonDimensions").textContent = `${carton.length_cm} × ${carton.width_cm} × ${carton.height_cm} cm`;
      field("predictedCartonName").textContent = carton.name;
    } catch (error) {
      if (sequence !== previewSequence) return;
      field("predictedCartonName").textContent = `预测失败：${error.message}`;
    }
  }

  async function loadCartons() {
    const catalog = field("cartonCatalog");
    try {
      const cartons = await api("/api/pricing/cartons");
      const rows = cartons.map((carton) => `<tr><td>${escapeHtml(carton.name)}</td><td>${carton.length_cm} × ${carton.width_cm} × ${carton.height_cm}</td><td>${carton.recommended_weight_g} g</td></tr>`).join("");
      catalog.innerHTML = `<table><thead><tr><th>包装型号</th><th>长 × 宽 × 高（cm）</th><th>建议重量（g）</th></tr></thead><tbody>${rows || '<tr><td colspan="3">纸箱目录为空</td></tr>'}</tbody></table>`;
    } catch (error) {
      catalog.textContent = `读取纸箱目录失败：${error.message}`;
    }
  }

  document.querySelectorAll("#pricing-workbench input, #pricing-workbench select").forEach((input) => {
    input.addEventListener("input", scheduleQuote);
  });
  field("productType").addEventListener("change", () => {
    const ordinary = field("productType").value === "ordinary";
    field("purchasePriceField").hidden = !ordinary;
    field("printedInputs").hidden = ordinary;
    field("ordinaryAdvanced").hidden = !ordinary;
    scheduleQuote();
  });
  ["productWeight", "productLength", "productWidth", "productHeight"].forEach((id) => field(id).addEventListener("input", schedulePreview));

  field("productType").dispatchEvent(new Event("change", { bubbles: true }));
  loadCartons();
  scheduleQuote();
})();
