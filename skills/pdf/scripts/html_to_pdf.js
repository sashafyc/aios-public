// html_to_pdf.js — generic HTML → PDF converter via Puppeteer (Chrome headless)
//
// Usage:
//   node html_to_pdf.js <input.html> [output.pdf] [--a4]
//
// По умолчанию: одна длинная страница (ширина A4 = 794px, высота = высота
// контента, без пагинации). Идеально для КП, лендингов и стилизованных
// документов с тёмной темой / base64-картинками — рендерится как скролл-сайт.
//
// Флаг --a4: обычная A4-пагинация с полями.
//
// Требует Node.js + puppeteer (тянет Chromium ~150MB):
//   npm install -g puppeteer    (или локально: npm install puppeteer)

const puppeteer = require("puppeteer");
const path = require("path");
const fs = require("fs");

(async () => {
  const args = process.argv.slice(2).filter((a) => a !== "--a4");
  const a4 = process.argv.includes("--a4");

  if (!args[0]) {
    console.error("Usage: node html_to_pdf.js <input.html> [output.pdf] [--a4]");
    process.exit(1);
  }

  const htmlPath = path.resolve(args[0]);
  const pdfPath = args[1]
    ? path.resolve(args[1])
    : htmlPath.replace(/\.html?$/, ".pdf");

  const browser = await puppeteer.launch({
    headless: "new",
    args: ["--no-sandbox", "--disable-setuid-sandbox"],
  });
  const page = await browser.newPage();
  await page.setViewport({ width: 794, height: 600 });
  await page.goto("file://" + htmlPath, { waitUntil: "networkidle0" });

  let pdfOpts = { path: pdfPath, printBackground: true };
  if (a4) {
    pdfOpts.format = "A4";
    pdfOpts.margin = { top: "12mm", right: "12mm", bottom: "12mm", left: "12mm" };
  } else {
    const height = await page.evaluate(() =>
      Math.max(document.body.scrollHeight, document.documentElement.scrollHeight)
    );
    pdfOpts.width = "794px";
    pdfOpts.height = height + 50 + "px";
    pdfOpts.margin = { top: "0", right: "0", bottom: "0", left: "0" };
  }

  await page.pdf(pdfOpts);
  await browser.close();

  const stats = fs.statSync(pdfPath);
  console.log("PDF: " + pdfPath + " (" + stats.size + " bytes)");
})();
