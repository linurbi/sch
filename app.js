const CART_KEY = "sch-order-cart-v1";
const NAMES_KEY = "sch-order-names-v1";
const SENDER_KEY = "sch-order-sender-v1";
// Without a right-to-left mark a line that opens with digits or latin letters
// flips direction in WhatsApp, which is what made the order hard to read.
const RLM = "\u200f";

const els = {
  search: document.getElementById("search"),
  brands: document.getElementById("brands"),
  grid: document.getElementById("grid"),
  status: document.getElementById("status"),
  cartToggle: document.getElementById("cartToggle"),
  cartCount: document.getElementById("cartCount"),
  drawer: document.getElementById("drawer"),
  backdrop: document.getElementById("backdrop"),
  closeCart: document.getElementById("closeCart"),
  cartItems: document.getElementById("cartItems"),
  copyWhatsapp: document.getElementById("copyWhatsapp"),
  copyTsv: document.getElementById("copyTsv"),
  downloadTxt: document.getElementById("downloadTxt"),
  clearCart: document.getElementById("clearCart"),
  preview: document.getElementById("preview"),
  sender: document.getElementById("sender"),
};

let products = [];
let brandFilter = "";
let cart = loadStore(CART_KEY);
let customNames = loadStore(NAMES_KEY);

function loadStore(key) {
  try {
    return JSON.parse(localStorage.getItem(key) || "{}");
  } catch {
    return {};
  }
}

function saveCart() {
  localStorage.setItem(CART_KEY, JSON.stringify(cart));
}

function saveNames() {
  localStorage.setItem(NAMES_KEY, JSON.stringify(customNames));
}

function senderName() {
  return (els.sender?.value || "").trim();
}

function displayName(product) {
  return (customNames[product.barcode] || product.name || "").trim();
}

function isUnnamed(product) {
  const name = displayName(product);
  return !name || name === product.brand.trim();
}

function setCustomName(barcode, name) {
  const clean = name.trim();
  if (clean) customNames[barcode] = clean;
  else delete customNames[barcode];
  saveNames();
}

function qtyOf(barcode) {
  return cart[barcode] || 0;
}

function setQty(barcode, qty) {
  if (qty <= 0) delete cart[barcode];
  else cart[barcode] = qty;
  saveCart();
  render();
}

function selectedItems() {
  return products
    .filter((p) => qtyOf(p.barcode) > 0)
    .map((p) => ({
      ...p,
      name: displayName(p) || p.brand || "מוצר",
      quantity: qtyOf(p.barcode),
    }));
}

function formatWhatsapp(items) {
  if (!items.length) return "אין מוצרים בהזמנה";
  const units = items.reduce((sum, item) => sum + item.quantity, 0);
  const from = senderName();
  const lines = ["*הזמנה שסטוביץ*"];
  if (from) lines.push(`${RLM}מאת: ${from}`);
  lines.push(`${RLM}${items.length} מוצרים, ${units} יחידות בסך הכל`);
  items.forEach((item, index) => {
    lines.push("");
    lines.push(`${RLM}*${index + 1}. ${item.name}*`);
    lines.push(`${RLM}ברקוד: ${item.barcode}`);
    lines.push(`${RLM}כמות: ${item.quantity}`);
  });
  return lines.join("\n");
}

function formatTsv(items) {
  const from = senderName();
  const rows = from ? [["מאת", from, ""]] : [];
  rows.push(["מוצר", "ברקוד", "כמות"]);
  for (const item of items) {
    rows.push([item.name, item.barcode, String(item.quantity)]);
  }
  return rows.map((row) => row.join("\t")).join("\n");
}

function toast(message) {
  const node = document.createElement("div");
  node.className = "toast";
  node.textContent = message;
  document.body.appendChild(node);
  setTimeout(() => node.remove(), 1800);
}

async function copyText(text) {
  await navigator.clipboard.writeText(text);
  toast("הועתק");
}

function downloadText(text) {
  const blob = new Blob([text], { type: "text/plain;charset=utf-8" });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = "sch-order.txt";
  a.click();
  URL.revokeObjectURL(url);
}

function uniqueBrands() {
  return [...new Set(products.map((p) => p.brand).filter(Boolean))].sort(
    (a, b) => a.localeCompare(b, "he")
  );
}

function visibleProducts() {
  const q = els.search.value.trim().toLowerCase();
  return products.filter((p) => {
    if (brandFilter && p.brand !== brandFilter) return false;
    if (!q) return true;
    return (
      displayName(p).toLowerCase().includes(q) ||
      p.barcode.includes(q) ||
      (p.sku && p.sku.includes(q))
    );
  });
}

function renderBrands() {
  const brands = uniqueBrands();
  els.brands.innerHTML = "";
  const all = chip("הכל", brandFilter === "");
  all.addEventListener("click", () => {
    brandFilter = "";
    render();
  });
  els.brands.appendChild(all);
  for (const brand of brands) {
    const node = chip(brand, brandFilter === brand);
    node.addEventListener("click", () => {
      brandFilter = brand;
      render();
    });
    els.brands.appendChild(node);
  }
}

function chip(label, active) {
  const button = document.createElement("button");
  button.type = "button";
  button.className = `chip${active ? " active" : ""}`;
  button.textContent = label;
  return button;
}

function qtyControls(barcode) {
  const wrap = document.createElement("div");
  wrap.className = "qty";
  const minus = document.createElement("button");
  minus.type = "button";
  minus.textContent = "−";
  minus.addEventListener("click", (e) => {
    e.stopPropagation();
    setQty(barcode, qtyOf(barcode) - 1);
  });
  const value = document.createElement("span");
  value.textContent = String(qtyOf(barcode));
  const plus = document.createElement("button");
  plus.type = "button";
  plus.textContent = "+";
  plus.addEventListener("click", (e) => {
    e.stopPropagation();
    setQty(barcode, qtyOf(barcode) + 1);
  });
  wrap.append(minus, value, plus);
  return wrap;
}

function renderGrid() {
  const list = visibleProducts();
  els.grid.innerHTML = "";
  els.status.textContent = `${list.length} מוצרים`;
  for (const product of list) {
    const card = document.createElement("article");
    card.className = "card";
    card.addEventListener("click", () => setQty(product.barcode, qtyOf(product.barcode) + 1));
    const img = document.createElement("img");
    img.src = product.image;
    img.alt = displayName(product);
    img.loading = "lazy";
    const body = document.createElement("div");
    body.className = "card-body";
    const title = document.createElement("h3");
    title.textContent = displayName(product);
    if (isUnnamed(product)) {
      title.classList.add("no-name");
      title.textContent = `${displayName(product)} — ללא שם בקטלוג`;
    }
    const code = document.createElement("div");
    code.className = "barcode";
    code.textContent = product.barcode;
    body.append(title, code, qtyControls(product.barcode));
    card.append(img, body);
    els.grid.appendChild(card);
  }
}

function renderCart() {
  const items = selectedItems();
  const total = items.reduce((sum, item) => sum + item.quantity, 0);
  els.cartCount.textContent = String(total);
  els.cartItems.innerHTML = "";
  if (!items.length) {
    els.cartItems.textContent = "עדיין לא נבחרו מוצרים.";
  } else {
    for (const item of items) {
      const row = document.createElement("div");
      row.className = "cart-row";
      const text = document.createElement("div");
      text.className = "cart-text";
      const name = document.createElement("input");
      name.className = "name-edit";
      name.value = item.name;
      name.title = "אפשר לתקן את שם המוצר לפני השליחה";
      name.addEventListener("change", () => {
        setCustomName(item.barcode, name.value);
        render();
      });
      const code = document.createElement("span");
      code.className = "barcode";
      code.textContent = item.barcode;
      text.append(name, code);
      row.append(text, qtyControls(item.barcode));
      els.cartItems.appendChild(row);
    }
  }
  els.preview.textContent = formatWhatsapp(items);
}

function render() {
  renderBrands();
  renderGrid();
  renderCart();
}

function openCart(open) {
  els.drawer.hidden = !open;
  els.backdrop.hidden = !open;
}

if (els.sender) {
  els.sender.value = localStorage.getItem(SENDER_KEY) || "";
  els.sender.addEventListener("input", () => {
    localStorage.setItem(SENDER_KEY, senderName());
    renderCart();
  });
}

els.search.addEventListener("input", render);
els.cartToggle.addEventListener("click", () => openCart(true));
els.closeCart.addEventListener("click", () => openCart(false));
els.backdrop.addEventListener("click", () => openCart(false));
els.copyWhatsapp.addEventListener("click", () => copyText(formatWhatsapp(selectedItems())));
els.copyTsv.addEventListener("click", () => copyText(formatTsv(selectedItems())));
els.downloadTxt.addEventListener("click", () => downloadText(formatWhatsapp(selectedItems())));
els.clearCart.addEventListener("click", () => {
  cart = {};
  saveCart();
  render();
});

try {
  const data = await fetch("data/products.json").then((r) => {
    if (!r.ok) throw new Error("missing products.json");
    return r.json();
  });
  products = data.products || [];
  render();
} catch (error) {
  els.status.textContent = "לא נמצא קובץ מוצרים. הריצו קודם: py -3 scripts/fetch_catalogs.py";
  console.error(error);
}
