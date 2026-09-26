import type { EvidenceItem, QueryResponse, Run } from '../types';

export interface StepView {
  /** 当前点亮的步骤（1..4）；0 表示还没开始 */
  index: number;
  /** 第 4 步的文案：无有效引用时不能写「对照引用」（27 号复审第 3 点） */
  step4Label: string;
  step4Pending: boolean;
}

export const STEP_LABELS = ['提问', '找证据', '生成回答', '对照引用'] as const;

/**
 * 步骤条只反映真实状态：
 * - 零结果 → 停在「找证据」；
 * - 生成失败 → 停在「生成回答」；
 * - 有回答但没有可用引用 → 第 4 步**不点亮**，改显示「引用检查：无可用引用」。
 */
export function deriveSteps(run: Run, response: QueryResponse | null): StepView {
  const ok: StepView = { index: 0, step4Label: STEP_LABELS[3], step4Pending: false };
  if (!response) {
    const index = run.status === 'retrieving' || run.status === 'generating' ? 2 : run.question ? 2 : 1;
    return { ...ok, index };
  }
  if (run.status === 'retrieving') return { ...ok, index: 2 };
  if (run.status === 'generating') return { ...ok, index: 3 };
  if (response.notice === 'zero_hits') return { ...ok, index: 2 };
  if (response.notice === 'generation_failed' || run.status === 'error') return { ...ok, index: 3 };
  if (response.refs.length > 0) return { ...ok, index: 4 };
  if (response.answer !== null) {
    return { index: 3, step4Label: '引用检查：无可用引用', step4Pending: true };
  }
  return { ...ok, index: 2 };
}

/** 把 refs（完整 chunk id）映射回片段对象，顺序按回答里首次出现的顺序 */
export function refItems(response: QueryResponse | null): EvidenceItem[] {
  if (!response || response.refs.length === 0) return [];
  const pool = new Map<string, EvidenceItem>();
  [...response.hits, ...response.neighbors].forEach((e) => pool.set(e.id, e));
  return response.refs.map((id) => pool.get(id)).filter((e): e is EvidenceItem => Boolean(e));
}

export function sourceLine(e: EvidenceItem): string {
  return [e.source_title || e.source_id, e.section, e.source_org].filter(Boolean).join(' · ');
}

export function clip(text: string, n = 96): string {
  const flat = text.replace(/\s+/g, ' ').trim();
  return flat.length <= n ? flat : `${flat.slice(0, n)}…`;
}
