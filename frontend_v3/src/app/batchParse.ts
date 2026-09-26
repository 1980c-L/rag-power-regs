/**
 * 批量输入解析（47 号方案 A1）—— 纯函数，无副作用。
 *
 * 只支持两种输入：
 *   - 文本框粘贴 / .txt：一行一个问题（UTF-8）；
 *   - .csv：必须存在 `question` 列（UTF-8 或 UTF-8 BOM）。
 * 明确不支持 XLSX / PDF / DOCX 作为问题输入，也不做问题自动生成。
 *
 * 这里做的校验（1–20 条、去空行、单条 ≤ 500 字符）**服务端会再做一遍**：
 * 页面这一层只是为了"在发出请求前就把问题说清楚"，不是信任边界。
 */
export const MAX_QUESTIONS = 20;
export const MIN_QUESTIONS = 1;
export const MAX_QUESTION_CHARS = 500;

export type ParseSource = 'text' | 'txt' | 'csv';

export interface ParseResult {
  questions: string[];
  /** null = 解析成功；否则是给用户看的原因（页面照原样显示，不美化） */
  error: string | null;
  source: ParseSource;
}

export function stripBom(text: string): string {
  return text.replace(/^\ufeff/, '');
}

export function splitLines(text: string): string[] {
  return stripBom(text).replace(/\r\n?/g, '\n').split('\n');
}

/** 逐条校验（与 batch_jobs.normalize_questions 同一口径） */
export function validateQuestions(questions: string[], source: ParseSource): ParseResult {
  if (questions.length < MIN_QUESTIONS) {
    return { questions: [], error: '至少要有一道非空问题（空行会被忽略）。', source };
  }
  if (questions.length > MAX_QUESTIONS) {
    return {
      questions: [],
      error: `一次最多 ${MAX_QUESTIONS} 道问题，当前 ${questions.length} 道。`,
      source,
    };
  }
  for (let i = 0; i < questions.length; i += 1) {
    if (questions[i].length > MAX_QUESTION_CHARS) {
      return {
        questions: [],
        error: `第 ${i + 1} 道问题超过 ${MAX_QUESTION_CHARS} 个字符（当前 ${questions[i].length}）。`,
        source,
      };
    }
  }
  return { questions, error: null, source };
}

/** 一行一个问题：去首尾空白、丢掉空行，**不做去重**（重复问题要分别执行） */
export function parsePastedText(raw: string, source: ParseSource = 'text'): ParseResult {
  const questions = splitLines(raw).map((line) => line.trim()).filter(Boolean);
  return validateQuestions(questions, source);
}

export function parseTxtFile(raw: string): ParseResult {
  return parsePastedText(raw, 'txt');
}

/**
 * 最小 CSV 解析（RFC 4180 风格）：支持双引号包裹、引号内逗号/换行、`""` 转义。
 * 不引入第三方依赖（47 号方案不授权新增运行时依赖）。
 */
export function parseCsvRows(raw: string): string[][] {
  const text = stripBom(raw);
  const rows: string[][] = [];
  let row: string[] = [];
  let field = '';
  let inQuotes = false;
  for (let i = 0; i < text.length; i += 1) {
    const ch = text[i];
    if (inQuotes) {
      if (ch === '"') {
        if (text[i + 1] === '"') { field += '"'; i += 1; } else { inQuotes = false; }
      } else {
        field += ch;
      }
      continue;
    }
    if (ch === '"') { inQuotes = true; continue; }
    if (ch === ',') { row.push(field); field = ''; continue; }
    if (ch === '\n') { row.push(field); rows.push(row); row = []; field = ''; continue; }
    if (ch === '\r') { continue; }
    field += ch;
  }
  if (field.length > 0 || row.length > 0) { row.push(field); rows.push(row); }
  return rows.filter((r) => r.some((cell) => cell.trim() !== ''));
}

export function parseCsv(raw: string): ParseResult {
  const rows = parseCsvRows(raw);
  if (rows.length === 0) {
    return { questions: [], error: 'CSV 文件是空的。', source: 'csv' };
  }
  const header = rows[0].map((cell) => cell.trim().toLowerCase());
  const col = header.indexOf('question');
  if (col < 0) {
    return {
      questions: [],
      error: `CSV 必须包含 question 列（当前表头：${rows[0].map((c) => c.trim()).join(', ') || '空'}）。`,
      source: 'csv',
    };
  }
  const questions = rows.slice(1).map((r) => (r[col] ?? '').trim()).filter(Boolean);
  return validateQuestions(questions, 'csv');
}

/** 单题状态 → 页面标签（口径与 batch_jobs.ITEM_* 一一对应，不新增状态） */
export const ITEM_STATUS_LABELS: Record<string, string> = {
  pending: '等待',
  running: '运行中',
  done: '完成',
  refused: '未找到资料依据（拒答）',
  failed: '失败',
  not_executed: '未执行（被取消或提前停止）',
};

export const JOB_STATE_LABELS: Record<string, string> = {
  PENDING: '排队中',
  RUNNING: '运行中',
  COMPLETED: '已完成',
  CANCELLED: '已取消',
  STOPPED_ON_ERROR: '遇系统性错误已停止',
};
