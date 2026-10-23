const selectionRuleForm = document.querySelector("#selection-rule-form");

selectionRuleForm?.addEventListener("submit", async (event) => {
  event.preventDefault();
  const values = new FormData(selectionRuleForm);
  const totalText = String(values.get("minimum_7d_increment_total") || "").trim();
  const daysText = String(values.get("minimum_positive_days") || "").trim();
  if (Boolean(totalText) !== Boolean(daysText)) {
    notify("请同时填写两个阈值，或同时留空以保持观察模式", true);
    return;
  }
  const payload = {
    minimum_7d_increment_total: totalText ? Number(totalText) : null,
    minimum_positive_days: daysText ? Number(daysText) : null,
  };
  const button = selectionRuleForm.querySelector("button[type=submit]");
  button.disabled = true;
  try {
    await jsonRequest("/api/selection-qualification/rule", {
      method: "PUT",
      body: JSON.stringify(payload),
    });
    notify("选品资格规则已保存");
    window.location.reload();
  } catch (error) {
    notify(error.message, true);
    button.disabled = false;
  }
});

document.querySelector("#selection-evaluate")?.addEventListener("click", async (event) => {
  const button = event.currentTarget;
  button.disabled = true;
  try {
    await jsonRequest("/api/selection-qualification/evaluate", { method: "POST" });
    notify("候选已重新评估");
    window.location.reload();
  } catch (error) {
    notify(error.message, true);
    button.disabled = false;
  }
});

document.querySelectorAll(".selection-retry").forEach((button) => {
  button.addEventListener("click", async () => {
    if (!window.confirm("请确认已检查失败原因，是否将此商品重新加入店小秘队列？")) return;
    button.disabled = true;
    try {
      await jsonRequest(`/api/integrations/dianxiaomi/handoffs/${button.dataset.handoffId}/resume`, {
        method: "POST",
      });
      notify("任务已重新排队");
      window.location.reload();
    } catch (error) {
      notify(error.message, true);
      button.disabled = false;
    }
  });
});

document.querySelector("#selection-queue-all")?.addEventListener("click", async (event) => {
  const button = event.currentTarget;
  button.disabled = true;
  try {
    const result = await jsonRequest("/api/selection-qualification/collect", { method: "POST" });
    notify(`已加入今日 IXSPY 队列：${result.queued_count} 个候选 / 共 ${result.candidate_count} 个`);
    window.location.reload();
  } catch (error) {
    notify(error.message, true);
    button.disabled = false;
  }
});
