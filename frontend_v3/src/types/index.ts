/**
 * 第三版前端的数据结构（28 号方案约定：settings / run / answer / evidence 四组）。
 *
 * 这里的字段就是第二阶段 `api_v3.py` 要返回的结构。第一阶段 mockService 与第二阶段
 * apiService 必须返回同一形状，页面组件不做任何改动就能换数据源。
 */

export type CorpusId = 'rag_learning' | 'power_demo' | 'user_library';
export type GenerateMode = 'retrieve_only' | 'generate';
export type EvidenceOrigin = 'hit' | 'neighbor';

/** 一组：设置（唯一一套控件；右下设置栏读这里，主区只显示只读摘要） */
export interface Settings {
  corpus_id: CorpusId;
  mode: GenerateMode;
  top_k: number;
}

export interface CorpusParams {
  chunk_max_chars: number;
  top_k_default: number;
  bm25_k1: number;
  bm25_b: number;
}

export interface CorpusMeta {
  corpus_id: CorpusId;
  name: string;
  note: string;
  source_count: number;
  chunk_count: number;
  total_chars: number;
  version: string;
  tokenizer: string;
  params: CorpusParams;
  chunking_note: string;
  license_note: string;
  /** 电力示例库为 true：不是正式规程 */
  is_sample_only: boolean;
  warning: string;
}

export interface MetaResponse {
  schema_version: number;
  /** mock = 第一阶段本地示例数据；api = 已接后端 */
  data_source: 'mock' | 'api';
  mock_note: string;
  /** 生成层是否已通过正式验证；当前恒为 false */
  generation_verified: boolean;
  unverified_note: string;
  /** null = 未接入服务端、无法检测（第一阶段如实表达，不假装已配置） */
  api_key_configured: boolean | null;
  /** Top-K 可选值；第一阶段每个值都有对应快照，控件不是摆设 */
  top_k_choices: number[];
  corpora: CorpusMeta[];
  default_corpus: CorpusId;
  /** 我的资料库（仅 api 模式；mock 模式恒为 null）。不进 corpora：不参与评测、不继承内置库指标 */
  user_library: UserLibraryMeta | null;
  /**
   * 批量提问能力（47 号方案；仅 api 模式有）。上限与"结构性事实"都由服务端给出，
   * 页面照原样显示：并发固定 1、零自动重试不是页面上的可调参数。
   * mock 模式下为 undefined → 批量面板如实显示"不可用"，不假装能跑。
   */
  batch?: BatchCapabilities | null;
}

/** ---------------- 批量提问（47 号方案阶段 A） ---------------- */

export interface BatchCapabilities {
  min_questions: number;
  max_questions: number;
  max_question_chars: number;
  top_k_range: number[];
  modes: GenerateMode[];
  concurrency: number;
  auto_retry: number;
  export_formats: string[];
  state_note: string;
}

export type BatchJobState = 'PENDING' | 'RUNNING' | 'COMPLETED' | 'CANCELLED' | 'STOPPED_ON_ERROR';

/** 单题状态：等待 / 运行中 / 完成 / 拒答 / 失败 / 未执行（服务端口径，页面不得改写） */
export type BatchItemStatus = 'pending' | 'running' | 'done' | 'refused' | 'failed' | 'not_executed';

export interface BatchRef {
  id: string;
  source_title: string;
  section: string;
  text: string;
  origin: EvidenceOrigin;
}

export interface BatchHit {
  id: string;
  source_title: string;
  section: string;
  score: number | null;
}

export interface BatchJobItem {
  sequence: number;
  question: string;
  status: BatchItemStatus;
  answer: string | null;
  refs: BatchRef[];
  ref_occurrences: number;
  hits: BatchHit[];
  neighbors_count: number;
  notice: string | null;
  error: string;
  elapsed_ms: { retrieve: number | null; generate: number | null };
}

export interface BatchJob {
  job_id: string;
  created_at: string;
  finished_at: string | null;
  corpus_id: CorpusId;
  corpus_name: string;
  mode: GenerateMode;
  top_k: number;
  state: BatchJobState;
  total: number;
  planned_calls: number;
  started_calls: number;
  succeeded: number;
  failed: number;
  refused: number;
  /** 结构性事实：批量执行里没有重试循环，这里恒为 0 */
  retries: number;
  cancel_requested: boolean;
  cancelled_at_seq: number | null;
  stop_reason: string;
  llm_model: string;
  llm_loopback: boolean;
  items: BatchJobItem[];
}

export interface BatchJobSummary {
  total: number;
  finished: number;
  done: number;
  refused: number;
  failed: number;
  not_executed: number;
  pending: number;
  running: number;
  planned_calls: number;
  started_calls: number;
  succeeded: number;
  retries: number;
}

export interface BatchJobResponse {
  job: BatchJob;
  summary: BatchJobSummary;
}

export interface BatchCreateRequest {
  corpus_id: CorpusId;
  mode: GenerateMode;
  top_k: number;
  questions: string[];
  /** 生成模式的二次确认值：必须与服务端重新计算的 N 完全一致（仅检索模式传 0） */
  confirm_calls: number;
}

/** ---------------- 我的资料库（40 号方案 v1：仅 .txt/.md + 粘贴文本） ---------------- */

export interface UserLibraryDoc {
  id: string;
  title: string;
  source_name: string;
  source_url: string;
  original_filename: string | null;
  imported_at: string;
  sha256: string;
  chars: number;
  encoding: string;
}

export interface UserLibraryLimits {
  max_file_bytes: number;
  max_batch_items: number;
  max_total_bytes: number;
  allowed_suffixes: string[];
}

export interface UserLibraryMeta {
  available: boolean;
  name: string;
  note: string;
  source_count: number;
  chunk_count: number;
  total_chars: number;
  fingerprint: string;
  total_bytes: number;
  limits: UserLibraryLimits;
  documents: UserLibraryDoc[];
}

export interface UserDocImportItem {
  kind: 'paste' | 'file_b64';
  /** paste 必带；file_b64 不带 */
  text?: string;
  /** file_b64 必带：文件原始字节的 base64（编码探测在服务端做，前端不解码） */
  data_b64?: string;
  title?: string;
  source_name?: string;
  source_url?: string;
  /** file_b64 必带：仅作展示信息，绝不用于存储路径 */
  original_filename?: string;
}

export interface UserDocImportResult {
  index: number;
  status: 'imported' | 'duplicate' | 'rejected';
  id?: string;
  title?: string;
  reason?: string;
  code?: string;
  chars?: number;
  encoding?: string;
  source_encoding?: string;
}

export interface UserLibraryImportResponse {
  results: UserDocImportResult[];
  library: UserLibraryMeta;
}

export interface UserLibraryReindexResponse {
  reindexed: boolean;
  fingerprint: string;
  chunk_count: number;
  consistency: { missing_documents: string[]; orphan_files: string[] };
  library: UserLibraryMeta;
}

/** 二组：运行（问题、当前状态、耗时） */
export type RunStatus = 'idle' | 'retrieving' | 'generating' | 'done' | 'error';

export interface Run {
  question: string;
  status: RunStatus;
}

/** 互斥的状态提示：同一时刻最多一种（25 号第 2 条） */
export type NoticeKind =
  | 'zero_hits'
  | 'no_api_key'
  | 'generation_failed'
  | 'retrieve_only'
  | null;

/** 四组：证据片段 */
export interface EvidenceItem {
  id: string;
  origin: EvidenceOrigin;
  /** 命中名次；补充片段记录其所属命中的名次 */
  rank: number | null;
  /** BM25 排序分；补充片段为 null（它不参与检索打分） */
  score: number | null;
  source_id: string;
  source_title: string;
  source_org: string;
  source_url: string;
  section: string;
  text: string;
}

export interface ContextChars {
  total: number;
  hit: number;
  max: number;
  radius: number;
}

/** 三组：回答 + 能对上的引用 id */
export interface QueryRequest extends Settings {
  question: string;
}

export interface QueryResponse {
  /** **本次结果**用的那一次请求参数；改设置后不会被改写，用来防止结果被新设置错误归属 */
  request: QueryRequest;
  /** 回答正文被记录时用的 Top-K（21 号真实运行），与 request.top_k 不同时要如实说明 */
  recorded_top_k: number;
  status: Exclude<RunStatus, 'idle' | 'retrieving'>;
  notice: NoticeKind;
  notice_detail: string;
  elapsed_ms: { retrieve: number | null; generate: number | null };
  hits: EvidenceItem[];
  neighbors: EvidenceItem[];
  context_chars: ContextChars | null;
  answer: string | null;
  /** 去重后的完整 chunk id，与回答正文里的 [id] 一一对应 */
  refs: string[];
  ref_occurrences: number;
  recorded_from: string;
}

export interface DataService {
  fetchMeta(settings: Settings): Promise<MetaResponse>;
  query(req: QueryRequest): Promise<QueryResponse>;
  /** 我的资料库四个方法只在 apiService 实现；mock 模式下不存在（页面如实显示"不可用"） */
  listUserDocuments?(): Promise<UserLibraryMeta>;
  importUserDocuments?(items: UserDocImportItem[]): Promise<UserLibraryImportResponse>;
  deleteUserDocument?(id: string): Promise<{ deleted: string; library: UserLibraryMeta }>;
  reindexUserLibrary?(): Promise<UserLibraryReindexResponse>;
  /** 批量提问四个方法同样只在 apiService 实现（mock 模式没有服务端） */
  createBatchJob?(req: BatchCreateRequest): Promise<BatchJobResponse>;
  getBatchJob?(jobId: string): Promise<BatchJobResponse>;
  cancelBatchJob?(jobId: string): Promise<BatchJobResponse>;
  /** 导出走浏览器直接下载：URL 由这一处拼装，页面不自己拼路径 */
  batchExportUrl?(jobId: string, format: 'csv' | 'docx'): string;
}

/** 七种可演示状态（28 号方案第一阶段） */
export type UiStateKey =
  | 'initial'
  | 'loading'
  | 'ok'
  | 'no-refs'
  | 'zero'
  | 'no-key'
  | 'failure';

export const UI_STATE_LABELS: Record<UiStateKey, string> = {
  initial: '初始待提问',
  loading: '检索中',
  'ok': '有命中、有回答、有有效引用',
  'no-refs': '有命中、回答无可用引用',
  zero: '零检索结果',
  'no-key': '无 API Key，只展示检索结果',
  failure: '模型调用失败，保留已找到的证据',
};
