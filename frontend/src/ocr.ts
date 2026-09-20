import { createWorker, OEM, PSM, type Worker } from 'tesseract.js';

export type OcrLanguage = 'eng' | 'chi_tra+eng';
export type OcrProgress = { stage: string; progress: number };
export type QualityHint = { label: string; value: string; message: string; attention: boolean };
export type PreparedImage = {
  previewUrl: string;
  width: number;
  height: number;
  processedWidth: number;
  processedHeight: number;
  hints: QualityHint[];
};

const MAX_BYTES = 20 * 1024 * 1024;
const MAX_PIXELS = 24_000_000;
const MAX_OCR_EDGE = 2400;
const MIN_OCR_EDGE = 1600;
const ALLOWED_TYPES = new Set(['image/png', 'image/jpeg', 'image/webp']);

/** No uploads or remote recognition. Only bundled same-origin OCR assets are fetched. */
function assetUrl(file = ''): string {
  const meta = import.meta as ImportMeta & { env?: { BASE_URL?: string } };
  const base = meta.env?.BASE_URL ?? '/';
  return new URL(`${base}ocr/${file}`, window.location.href).href;
}

async function decodeImage(file: Blob): Promise<HTMLImageElement> {
  const url = URL.createObjectURL(file);
  try {
    const image = new Image();
    image.src = url;
    await image.decode();
    return image;
  } catch {
    throw new Error('無法讀取這張圖片，請重新匯出成 PNG、JPEG 或 WebP。');
  } finally {
    URL.revokeObjectURL(url);
  }
}

function canvas2d(width: number, height: number) {
  const canvas = document.createElement('canvas');
  canvas.width = width;
  canvas.height = height;
  const context = canvas.getContext('2d', { willReadFrequently: true });
  if (!context) throw new Error('此瀏覽器無法處理圖片，請改用新版瀏覽器。');
  context.fillStyle = '#fff';
  context.fillRect(0, 0, width, height);
  return { canvas, context };
}

/** Heuristics only: brightness and Laplacian variance cannot prove document validity. */
export async function prepareImage(file: File): Promise<PreparedImage> {
  if (file.type === 'application/pdf' || /\.pdf$/i.test(file.name)) {
    throw new Error('文字辨識只接受圖片。請先把 PDF 所需頁面轉成 PNG 或 JPEG，再逐頁辨識。');
  }
  if (!ALLOWED_TYPES.has(file.type)) throw new Error('請選擇 PNG、JPEG 或 WebP 圖片。');
  if (file.size > MAX_BYTES) throw new Error('圖片超過 20 MiB，請先縮小圖片再試。');
  if (!file.size) throw new Error('這是空白檔案，請重新選擇。');
  const image = await decodeImage(file);
  const width = image.naturalWidth;
  const height = image.naturalHeight;
  if (!width || !height || width * height > MAX_PIXELS) {
    throw new Error('圖片尺寸太大或無法辨識；最多接受 2,400 萬像素，請先縮小圖片。');
  }

  const scale = Math.min(1, MAX_OCR_EDGE / Math.max(width, height));
  const processedWidth = Math.max(1, Math.round(width * scale));
  const processedHeight = Math.max(1, Math.round(height * scale));
  const normalized = canvas2d(processedWidth, processedHeight);
  normalized.context.drawImage(image, 0, 0, processedWidth, processedHeight);
  // A data URL survives component unmounting, so a parent can retain its preview.
  // Canvas normalization also strips file metadata; nothing is uploaded or persisted here.
  const previewUrl = normalized.canvas.toDataURL('image/png');

  const sampleScale = Math.min(1, 900 / Math.max(width, height));
  const sampleWidth = Math.max(3, Math.round(width * sampleScale));
  const sampleHeight = Math.max(3, Math.round(height * sampleScale));
  const sample = canvas2d(sampleWidth, sampleHeight);
  sample.context.drawImage(image, 0, 0, sampleWidth, sampleHeight);
  const pixels = sample.context.getImageData(0, 0, sampleWidth, sampleHeight).data;
  const gray = new Float32Array(sampleWidth * sampleHeight);
  let luminanceSum = 0;
  for (let i = 0; i < gray.length; i++) {
    const at = i * 4;
    gray[i] = 0.2126 * pixels[at] + 0.7152 * pixels[at + 1] + 0.0722 * pixels[at + 2];
    luminanceSum += gray[i];
  }
  const mean = luminanceSum / gray.length;
  let edgeSum = 0;
  let edgeSquaredSum = 0;
  let edgeCount = 0;
  for (let y = 1; y < sampleHeight - 1; y++) {
    for (let x = 1; x < sampleWidth - 1; x++) {
      const i = y * sampleWidth + x;
      const edge = 4 * gray[i] - gray[i - 1] - gray[i + 1] - gray[i - sampleWidth] - gray[i + sampleWidth];
      edgeSum += edge;
      edgeSquaredSum += edge * edge;
      edgeCount++;
    }
  }
  const sharpness = Math.max(0, edgeSquaredSum / edgeCount - (edgeSum / edgeCount) ** 2);
  const lowResolution = Math.min(width, height) < 700;
  const exposureAttention = mean < 65 || mean > 242;
  const hints: QualityHint[] = [
    {
      label: '圖片尺寸', value: `${width} × ${height} px`, attention: lowResolution,
      message: lowResolution ? '短邊較小，小字可能辨識不完整。可改拍近一點、保持整份文件完整。' : '圖片尺寸可供辨識；仍須目視確認小字是否清楚。',
    },
    {
      label: '整體明暗', value: `平均亮度 ${Math.round(mean)} / 255`, attention: exposureAttention,
      message: mean < 65 ? '整體偏暗，請檢查文字是否陷入陰影。' : mean > 242 ? '整體偏亮；白紙背景也會有此結果，請目視檢查文字是否被反光吃掉。' : '未出現極端平均亮度；局部反光、陰影仍需人工查看。',
    },
    {
      label: '邊緣清晰度提示', value: `邊緣變化值 ${Math.round(sharpness)}`, attention: sharpness < 45,
      message: sharpness < 45 ? '邊緣變化較少，可能是模糊、空白或文字很少。請放大確認後再決定是否重拍。' : '可偵測到邊緣變化；印章、紋理也會提高此值，不能據此保證文字清楚。',
    },
  ];
  return { previewUrl, width, height, processedWidth, processedHeight, hints };
}

function statusLabel(status: string): string {
  const known: Record<string, string> = {
    'loading tesseract core': '載入文字辨識功能',
    'initializing tesseract': '啟動文字辨識功能',
    'loading language traineddata': '載入辨識語言資料',
    'initializing api': '準備文字辨識',
    'recognizing text': '正在辨識圖片文字',
  };
  return known[status] ?? '正在準備文字辨識';
}

/**
 * Enlarge a small image and drop its colour before recognition.
 *
 * A phone crop of an identity card is often around 300-500 px wide, which
 * leaves a Chinese glyph roughly 12 px tall -- below what the engine resolves.
 * Measured on the Ministry of the Interior specimen card: at its native 305 px
 * the address label and both address lines came back as noise, and at 1600 px
 * with the colour removed both were read. The colour matters because the card
 * is printed on a pink guilloche with red rules that survive the engine's own
 * thresholding and merge into the characters sitting on them.
 *
 * Images already at or above the threshold are returned untouched, so nothing
 * that reads correctly today changes. Only the pixels handed to the engine are
 * affected; the preview the applicant sees keeps its colour and its own size.
 */
async function enlargeForRecognition(source: string): Promise<string> {
  const image = new Image();
  image.src = source;
  await image.decode();
  const longest = Math.max(image.naturalWidth, image.naturalHeight);
  if (!longest || longest >= MIN_OCR_EDGE) return source;
  const scale = MIN_OCR_EDGE / longest;
  const width = Math.max(1, Math.round(image.naturalWidth * scale));
  const height = Math.max(1, Math.round(image.naturalHeight * scale));
  const { canvas, context } = canvas2d(width, height);
  context.imageSmoothingEnabled = true;
  context.imageSmoothingQuality = 'high';
  context.drawImage(image, 0, 0, width, height);
  const pixels = context.getImageData(0, 0, width, height);
  const data = pixels.data;
  for (let index = 0; index < data.length; index += 4) {
    const grey = Math.round(0.299 * data[index] + 0.587 * data[index + 1] + 0.114 * data[index + 2]);
    data[index] = grey;
    data[index + 1] = grey;
    data[index + 2] = grey;
  }
  context.putImageData(pixels, 0, 0);
  return canvas.toDataURL('image/png');
}

export async function recognizeLocally(
  image: string,
  language: OcrLanguage,
  onProgress: (progress: OcrProgress) => void,
  signal?: AbortSignal,
): Promise<{ text: string; confidence: number }> {
  if (signal?.aborted) throw new DOMException('辨識已取消', 'AbortError');
  let worker: Worker | undefined;
  let cancelled = false;
  let rejectAborted: (error: Error) => void = () => undefined;
  const interrupted = new Promise<never>((_, reject) => { rejectAborted = reject; });
  const stopWorker = () => {
    const running = worker;
    worker = undefined;
    if (running) void running.terminate().catch(() => undefined);
  };
  const abort = () => {
    cancelled = true;
    stopWorker();
    rejectAborted(new DOMException('辨識已取消', 'AbortError'));
  };
  signal?.addEventListener('abort', abort, { once: true });
  // Bound pathological input / stalled model loading. No data is sent as a fallback.
  const timeout = window.setTimeout(() => {
    cancelled = true;
    stopWorker();
    rejectAborted(new Error('辨識超過 2 分鐘，請縮小圖片、一次只保留一頁後重試。'));
  }, 120_000);
  try {
    onProgress({ stage: '準備文字辨識資料', progress: 0 });
    const creation = createWorker(language, OEM.LSTM_ONLY, {
      workerPath: assetUrl('worker.min.js'),
      corePath: assetUrl(),
      langPath: assetUrl(),
      workerBlobURL: false,
      gzip: false,
      cacheMethod: 'none',
      logger: ({ status, progress }) => {
        if (!cancelled) onProgress({ stage: statusLabel(status), progress: Math.max(0, Math.min(1, progress || 0)) });
      },
      errorHandler: () => {
        cancelled = true;
        stopWorker();
        rejectAborted(new Error('文字辨識功能無法載入，請重新整理頁面後再試。'));
      },
    });
    // If cancelled while initializing, terminate as soon as the worker becomes available.
    void creation.then((created) => { if (cancelled) void created.terminate(); }).catch(() => undefined);
    worker = await Promise.race([creation, interrupted]);
    if (cancelled) throw new DOMException('辨識已取消', 'AbortError');
    await Promise.race([worker.setParameters({ tessedit_pageseg_mode: PSM.AUTO, preserve_interword_spaces: '1' }), interrupted]);
    // A failed enlargement must not lose the image; fall back to the original pixels.
    const pixels = await Promise.race([enlargeForRecognition(image).catch(() => image), interrupted]);
    if (cancelled) throw new DOMException('辨識已取消', 'AbortError');
    const result = await Promise.race([worker.recognize(pixels, {}, { text: true }), interrupted]);
    onProgress({ stage: '辨識完成，請對照原圖確認', progress: 1 });
    return { text: result.data.text.trim(), confidence: result.data.confidence };
  } finally {
    window.clearTimeout(timeout);
    signal?.removeEventListener('abort', abort);
    stopWorker();
  }
}

/**
 * A practice subscription invoice, laid out the way the real vendor invoices the
 * office actually receives are: header block, a billed-to block, a line-item
 * table, then a totals column on the right.
 *
 * Synthetic pixels, NOT a prefilled OCR result -- the engine must read this
 * canvas like any other image. Every name, address, mailbox and number below is
 * invented; a real invoice carries the applicant's own address and mailbox and
 * must never be built into the product.
 */
export async function createDemoReceipt(): Promise<File> {
  const { canvas, context } = canvas2d(1240, 1750);
  const text = (value: string, x: number, y: number, font: string, colour = '#111') => {
    context.font = font;
    context.fillStyle = colour;
    context.fillText(value, x, y);
  };
  const right = (value: string, x: number, y: number, font: string, colour = '#111') => {
    context.font = font;
    context.fillStyle = colour;
    context.fillText(value, x - context.measureText(value).width, y);
  };
  const rule = (y: number) => {
    context.strokeStyle = '#d8d8d8';
    context.lineWidth = 2;
    context.beginPath();
    context.moveTo(90, y);
    context.lineTo(1150, y);
    context.stroke();
  };

  text('Invoice', 90, 130, 'bold 62px sans-serif');
  const head: Array<[string, string]> = [
    ['Invoice number', 'PRACTICE-0000-0000'],
    ['Date of issue', 'September 7, 2026'],
    ['Date due', 'September 7, 2026'],
    ['VAT Registration', 'Taiwan VAT: 00000000'],
  ];
  head.forEach(([label, value], index) => {
    text(label, 90, 210 + index * 44, 'bold 28px sans-serif', '#222');
    text(value, 400, 210 + index * 44, '28px sans-serif');
  });

  text('Practice Software, PBC', 90, 450, 'bold 28px sans-serif');
  ['548 Practice Street', 'PMB 00000', 'Practice City, CA 00000', 'United States',
    'support@example.test'].forEach((line, index) =>
    text(line, 90, 496 + index * 40, '28px sans-serif', '#333'));

  text('Bill to', 640, 450, 'bold 28px sans-serif');
  ['練習使用者', '新竹市東區練習路100號', 'Taiwan', 'practice@example.test'].forEach(
    (line, index) => text(line, 640, 496 + index * 40, '28px sans-serif', '#333'));

  text('TWD 650.00 due September 7, 2026', 90, 790, 'bold 44px sans-serif');

  rule(880);
  text('Description', 90, 862, '26px sans-serif', '#555');
  right('Qty', 800, 862, '26px sans-serif', '#555');
  right('Unit price', 960, 862, '26px sans-serif', '#555');
  right('Amount', 1150, 862, '26px sans-serif', '#555');

  text('Practice AI Pro', 90, 936, '30px sans-serif');
  text('Sep 7 - Oct 7, 2026', 90, 980, '28px sans-serif', '#444');
  right('1', 800, 936, '30px sans-serif');
  right('TWD 650.00', 960, 936, '30px sans-serif');
  right('TWD 650.00', 1150, 936, '30px sans-serif');

  const totals: Array<[string, string, boolean]> = [
    ['Subtotal', 'TWD 650.00', false],
    ['Total excluding tax', 'TWD 650.00', false],
    ['Total', 'TWD 650.00', false],
    ['Amount due', 'TWD 650.00', true],
  ];
  totals.forEach(([label, value, bold], index) => {
    const y = 1100 + index * 56;
    rule(y - 40);
    text(label, 640, y, `${bold ? 'bold ' : ''}30px sans-serif`, '#333');
    right(value, 1150, y, `${bold ? 'bold ' : ''}30px sans-serif`);
  });
  rule(1330);

  text('Plan: Monthly subscription', 90, 1420, '30px sans-serif');
  text('Payment method: Practice card', 90, 1470, '30px sans-serif');

  rule(1560);
  text('練習收據・非正式憑證', 90, 1630, 'bold 34px sans-serif', '#b03027');
  text('For text recognition practice only. Not an invoice.', 90, 1684, '27px sans-serif', '#444');
  text('不能作為付款、金額或申請資格的證明。', 90, 1726, '27px sans-serif', '#444');

  const blob = await new Promise<Blob>((resolve, reject) => {
    canvas.toBlob((value) => value ? resolve(value) : reject(new Error('無法建立練習圖片。')), 'image/png');
  });
  return new File([blob], 'practice-invoice.png', { type: 'image/png' });
}
