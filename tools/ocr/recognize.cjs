const fs = require('node:fs');
const path = require('node:path');
const { createWorker } = require('tesseract.js');

async function main() {
  const [input, output, cache] = process.argv.slice(2);
  const languagePath = path.join(cache, 'languages');
  fs.mkdirSync(languagePath, { recursive: true });
  for (const language of ['rus', 'eng']) {
    const source = path.join(__dirname, 'node_modules', '@tesseract.js-data', language,
      '4.0.0_best_int', `${language}.traineddata.gz`);
    const target = path.join(languagePath, `${language}.traineddata.gz`);
    if (!fs.existsSync(target)) fs.copyFileSync(source, target, fs.constants.COPYFILE_EXCL);
  }
  // Every engine and language resource is local. No URL is accepted from a document.
  const worker = await createWorker('rus+eng', 1, {
    langPath: languagePath, cachePath: cache, gzip: true,
  });
  try {
    const { data } = await worker.recognize(input);
    fs.writeFileSync(output, JSON.stringify({ text: data.text.slice(0, 2000000), confidence: data.confidence }));
  } finally { await worker.terminate(); }
}
main().catch(() => { process.stderr.write('Local OCR failed\n'); process.exitCode = 1; });
