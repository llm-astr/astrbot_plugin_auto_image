/* 提示词图库管理页：通过 window.AstrBotPluginPage bridge 调用插件后端 API */

const bridge = window.AstrBotPluginPage;

const $ = (id) => document.getElementById(id);
const grid = $("grid");
const emptyEl = $("empty");
const toastEl = $("toast");
const kwInput = $("kw-input");
const fileInput = $("file-input");
const fileName = $("file-name");
const preview = $("preview");
const addBtn = $("add-btn");

let toastTimer = null;
let items = [];

function t(key, fallback) {
  try {
    return bridge.t(key, fallback);
  } catch (e) {
    return fallback;
  }
}

function toast(msg, isError) {
  toastEl.textContent = msg;
  toastEl.classList.remove("hidden");
  toastEl.classList.toggle("error", !!isError);
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => toastEl.classList.add("hidden"), 3200);
}

function parseKeywords(text) {
  return String(text || "")
    .split(/[\s,，、]+/)
    .map((s) => s.trim())
    .filter(Boolean);
}

function fmtTime(ts) {
  if (!ts) return "";
  const d = new Date(ts * 1000);
  const pad = (n) => String(n).padStart(2, "0");
  return `${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

async function refresh() {
  try {
    const data = await bridge.apiGet("gallery/list");
    items = (data && data.items) || [];
    render();
  } catch (e) {
    toast(t("pages.gallery.load_fail", "加载失败：") + e.message, true);
  }
}

function render() {
  grid.innerHTML = "";
  emptyEl.classList.toggle("hidden", items.length > 0);
  items.forEach((item, idx) => grid.appendChild(renderCard(item, idx)));
}

function renderCard(item, idx) {
  const card = document.createElement("div");
  card.className = "card item-card";

  const ord = document.createElement("div");
  ord.className = "ord";
  ord.textContent = `#${idx + 1}`;
  card.appendChild(ord);

  const imgBox = document.createElement("div");
  imgBox.className = "thumb";
  if (item.has_image) {
    imgBox.textContent = "…";
    bridge
      .apiGet("gallery/thumb/" + item.id)
      .then((d) => {
        if (d && d.data_url) {
          imgBox.textContent = "";
          const img = document.createElement("img");
          img.src = d.data_url;
          img.alt = (item.keywords || []).join(" ");
          imgBox.appendChild(img);
        }
      })
      .catch(() => {
        imgBox.textContent = "×";
      });
  } else {
    imgBox.textContent = t("pages.gallery.no_image", "未上传图片");
    imgBox.classList.add("no-image");
  }
  card.appendChild(imgBox);

  const kwBox = document.createElement("div");
  kwBox.className = "kws";
  (item.keywords || []).forEach((kw) => {
    const chip = document.createElement("span");
    chip.className = "chip";
    chip.textContent = kw;
    kwBox.appendChild(chip);
  });
  if (!(item.keywords || []).length) {
    const chip = document.createElement("span");
    chip.className = "chip muted";
    chip.textContent = t("pages.gallery.no_kw", "未设置提示词");
    kwBox.appendChild(chip);
  }
  card.appendChild(kwBox);

  const time = document.createElement("div");
  time.className = "time";
  time.textContent = fmtTime(item.ts);
  card.appendChild(time);

  // 编辑区（默认隐藏）
  const editBox = document.createElement("div");
  editBox.className = "edit-box hidden";
  const editInput = document.createElement("input");
  editInput.type = "text";
  editInput.value = (item.keywords || []).join(" ");
  const saveBtn = document.createElement("button");
  saveBtn.className = "btn small primary";
  saveBtn.textContent = t("pages.gallery.save", "保存");
  const cancelBtn = document.createElement("button");
  cancelBtn.className = "btn small";
  cancelBtn.textContent = t("pages.gallery.cancel", "取消");
  editBox.appendChild(editInput);
  editBox.appendChild(saveBtn);
  editBox.appendChild(cancelBtn);
  card.appendChild(editBox);

  const ops = document.createElement("div");
  ops.className = "ops";
  const editBtn = document.createElement("button");
  editBtn.className = "btn small";
  editBtn.textContent = t("pages.gallery.edit", "编辑提示词");
  const delBtn = document.createElement("button");
  delBtn.className = "btn small danger";
  delBtn.textContent = t("pages.gallery.delete", "删除");
  ops.appendChild(editBtn);
  ops.appendChild(delBtn);
  card.appendChild(ops);

  editBtn.addEventListener("click", () => {
    editBox.classList.toggle("hidden");
    editInput.focus();
  });
  cancelBtn.addEventListener("click", () => {
    editBox.classList.add("hidden");
    editInput.value = (item.keywords || []).join(" ");
  });
  saveBtn.addEventListener("click", async () => {
    const kws = parseKeywords(editInput.value);
    if (!kws.length) {
      toast(t("pages.gallery.need_kw", "至少需要 1 个预设提示词"), true);
      return;
    }
    saveBtn.disabled = true;
    try {
      await bridge.apiPost("gallery/update", { id: item.id, keywords: kws });
      toast(t("pages.gallery.saved", "已保存"));
      await refresh();
    } catch (e) {
      toast(e.message, true);
    } finally {
      saveBtn.disabled = false;
    }
  });

  // 删除：两段式确认（iframe 沙箱不允许 confirm 弹窗）
  let armed = false;
  let armTimer = null;
  delBtn.addEventListener("click", async () => {
    if (!armed) {
      armed = true;
      delBtn.textContent = t("pages.gallery.confirm_delete", "再点一次确认删除");
      delBtn.classList.add("armed");
      armTimer = setTimeout(() => {
        armed = false;
        delBtn.textContent = t("pages.gallery.delete", "删除");
        delBtn.classList.remove("armed");
      }, 3000);
      return;
    }
    clearTimeout(armTimer);
    delBtn.disabled = true;
    try {
      await bridge.apiPost("gallery/delete", { id: item.id });
      toast(t("pages.gallery.deleted", "已删除"));
      await refresh();
    } catch (e) {
      toast(e.message, true);
      delBtn.disabled = false;
    }
  });

  return card;
}

// ---------------- 添加流程 ----------------

$("pick-btn").addEventListener("click", () => fileInput.click());
fileInput.addEventListener("change", () => {
  const f = fileInput.files && fileInput.files[0];
  if (!f) {
    fileName.textContent = t("pages.gallery.no_file", "未选择");
    preview.classList.add("hidden");
    return;
  }
  fileName.textContent = `${f.name}（${(f.size / 1024 / 1024).toFixed(2)}MB）`;
  const reader = new FileReader();
  reader.onload = () => {
    preview.src = reader.result;
    preview.classList.remove("hidden");
  };
  reader.readAsDataURL(f);
});

addBtn.addEventListener("click", async () => {
  const kws = parseKeywords(kwInput.value);
  const f = fileInput.files && fileInput.files[0];
  if (!kws.length) {
    toast(t("pages.gallery.need_kw", "至少需要 1 个预设提示词"), true);
    return;
  }
  if (!f) {
    toast(t("pages.gallery.need_img", "请选择要上传的图片"), true);
    return;
  }
  if (f.size > 15 * 1024 * 1024) {
    toast(t("pages.gallery.too_big", "图片超过 15MB 上限"), true);
    return;
  }
  addBtn.disabled = true;
  addBtn.textContent = t("pages.gallery.adding", "添加中……");
  let createdId = null;
  try {
    const created = await bridge.apiPost("gallery/create", { keywords: kws });
    createdId = created && created.item && created.item.id;
    if (!createdId) throw new Error(t("pages.gallery.create_fail", "创建条目失败"));
    await bridge.upload("gallery/upload/" + createdId, f);
    toast(t("pages.gallery.added", "已添加到图库"));
    kwInput.value = "";
    fileInput.value = "";
    fileName.textContent = t("pages.gallery.no_file", "未选择");
    preview.classList.add("hidden");
    await refresh();
  } catch (e) {
    // 上传失败时回滚已创建的空条目，避免残留无图条目
    if (createdId) {
      try {
        await bridge.apiPost("gallery/delete", { id: createdId });
      } catch (e2) {
        /* 忽略回滚失败 */
      }
    }
    toast(t("pages.gallery.add_fail", "添加失败：") + e.message, true);
  } finally {
    addBtn.disabled = false;
    addBtn.textContent = t("pages.gallery.add", "添加到图库");
  }
});

$("refresh-btn").addEventListener("click", refresh);

// 回车提交添加
kwInput.addEventListener("keydown", (e) => {
  if (e.key === "Enter") addBtn.click();
});

// ---------------- 初始化 ----------------

function renderTexts() {
  document.title = t("pages.gallery.title", "提示词图库");
  $("page-title").textContent = t("pages.gallery.title", "提示词图库");
  $("page-desc").textContent = t(
    "pages.gallery.desc",
    "为每张图片绑定预设提示词；生图 / 改图要求中提到这些词时，机器人会自动带上对应图片作为参考图，并在提示词里按序数强调「第几张是什么」。"
  );
}

await bridge.ready();
renderTexts();
bridge.onContext(renderTexts);
await refresh();
