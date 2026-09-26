/**
 * 第一阶段数据源：本地示例数据。
 *
 * 数据来自 `tools/build_frontend_mocks.py` 生成的快照（真实 rag_core 检索结果 +
 * 21 号已记录的真实回答原文），**没有编造 BM25 分数、引用 id 或资料数量**。
 * 但第一阶段确实**没有接入真实语料与模型调用**，页面必须如实标注（见 TopBar 的示例数据标签）。
 *
 * 30 号复审 P1 的修法：快照为 **Top-K = 1..5 各生成一份**，页面选了几就用哪一份算出来的
 * 命中 / 同节补充 / 上下文预算 / 引用解析，控件不再只是"改标签"。
 *
 * 换成 apiService 时页面组件不需要改：返回结构完全一致。
 */
import snapshot from '../mocks/api_snapshots.json';
import type {
  CorpusId,
  DataService,
  MetaResponse,
  QueryRequest,
  QueryResponse,
  Settings,
  UiStateKey,
  EvidenceItem,
} from '../types';

type ScenarioKey = 'learning_triad' | 'power_grounding' | 'learning_no_refs' | 'power_zero';

/** 某一个 Top-K 下的完整结果 */
interface Variant {
  hits: EvidenceItem[];
  neighbors: EvidenceItem[];
  answer: string | null;
  refs: string[];
  ref_occurrences: number;
  context_chars: { total: number; hit: number; max: number; radius: number };
  elapsed_ms: { retrieve: number | null; generate: number | null };
  recorded_from: string;
}

interface Scenario {
  request: QueryRequest;
  recorded_top_k: number;
  by_top_k: Record<string, Variant>;
}

const SNAPSHOT = snapshot as unknown as {
  data_source: 'mock';
  mock_note: string;
  generation_verified: boolean;
  unverified_note: string;
  api_key_configured_default: boolean | null;
  top_k_choices: number[];
  corpora: MetaResponse['corpora'];
  default_corpus: CorpusId;
  scenarios: Record<ScenarioKey, Scenario>;
  answers_source: string;
};

/** 演示用延时：让"检索中"状态真实可见，**不作为耗时上报**（上报的耗时只有记录值） */
const RETRIEVE_MS = 320;
const GENERATE_MS = 420;

const delay = (ms: number) => new Promise<void>((r) => setTimeout(r, ms));

/** 问题路由：只有这几组真实快照，其它问题按语料走"无可用引用/零结果"如实表达 */
function pickScenario(req: QueryRequest): ScenarioKey {
  if (req.corpus_id === 'rag_learning') {
    return req.question.includes('三元组') ? 'learning_triad' : 'learning_no_refs';
  }
  return req.question.includes('接地线') ? 'power_grounding' : 'power_zero';
}

function variantOf(sc: Scenario, topK: number): Variant {
  return sc.by_top_k[String(topK)] ?? sc.by_top_k[String(sc.recorded_top_k)];
}

function buildResponse(req: QueryRequest, scenarioKey: ScenarioKey): QueryResponse {
  const sc = SNAPSHOT.scenarios[scenarioKey];
  const v = variantOf(sc, req.top_k);
  const zeroHits = v.hits.length === 0;
  const wantGenerate = req.mode === 'generate';
  const keyStatus = currentKeyStatus();
  const canGenerate = wantGenerate && !zeroHits && keyStatus !== false;

  const answer = canGenerate ? v.answer : null;
  const showNeighbors = canGenerate;              // 没走到生成调用就不展示生成上下文

  let notice: QueryResponse['notice'] = null;
  if (zeroHits) notice = 'zero_hits';
  else if (wantGenerate && keyStatus === false) notice = 'no_api_key';
  else if (!wantGenerate) notice = 'retrieve_only';

  return {
    request: req,
    recorded_top_k: sc.recorded_top_k,
    status: 'done',
    notice,
    notice_detail: '',
    elapsed_ms: { retrieve: null, generate: canGenerate ? v.elapsed_ms.generate : null },
    hits: v.hits,
    neighbors: showNeighbors ? v.neighbors : [],
    context_chars: showNeighbors ? v.context_chars : null,
    answer,
    refs: answer ? v.refs : [],
    ref_occurrences: answer ? v.ref_occurrences : 0,
    recorded_from: v.recorded_from,
  };
}

export function readForcedState(search: string): UiStateKey | null {
  const v = new URLSearchParams(search).get('state');
  const allowed: UiStateKey[] = ['initial', 'loading', 'ok', 'no-refs', 'zero', 'no-key', 'failure'];
  return allowed.includes(v as UiStateKey) ? (v as UiStateKey) : null;
}

// 强制演示状态从地址栏直接读一次并缓存：服务层不依赖组件生命周期
let cachedForcedState: UiStateKey | null | undefined;

function currentForcedState(): UiStateKey | null {
  if (cachedForcedState === undefined) cachedForcedState = readForcedState(window.location.search);
  return cachedForcedState;
}

/**
 * Key 状态：第一阶段没有服务端可检测，默认只能是 `null`（未知），
 * 只有演示「无 Key」状态时才显式为 false。**不伪造「已检测到」**。
 */
function currentKeyStatus(): boolean | null {
  return currentForcedState() === 'no-key' ? false : SNAPSHOT.api_key_configured_default;
}

function forcedScenario(state: UiStateKey): ScenarioKey {
  switch (state) {
    case 'ok': return 'learning_triad';
    case 'no-refs': return 'learning_no_refs';
    case 'zero': return 'power_zero';
    case 'no-key': return 'learning_triad';
    case 'failure': return 'learning_triad';
    default: return 'learning_triad';
  }
}

export function forcedSettings(state: UiStateKey): Settings {
  const sc = SNAPSHOT.scenarios[forcedScenario(state)];
  return { corpus_id: sc.request.corpus_id, mode: 'generate', top_k: sc.request.top_k };
}

export function forcedQuestion(state: UiStateKey): string {
  // 「初始待提问」状态必须是空输入框，否则步骤条会以为已经提过问
  if (state === 'initial') return '';
  return SNAPSHOT.scenarios[forcedScenario(state)].request.question;
}

/** 强制状态下的完整应答（failure 用错误状态 + 保留证据表达） */
export function forcedResponse(state: UiStateKey): QueryResponse | null {
  if (state === 'initial' || state === 'loading') return null;
  const req: QueryRequest = { ...forcedSettings(state), question: forcedQuestion(state) };
  const res = buildResponse(req, forcedScenario(state));
  if (state === 'failure') {
    return {
      ...res,
      status: 'error',
      notice: 'generation_failed',
      notice_detail: '示例数据：模型调用失败（演示状态），已保留检索到的资料。',
      answer: null,
      refs: [],
      ref_occurrences: 0,
    };
  }
  return res;
}

export const mockService: DataService = {
  async fetchMeta(_settings: Settings): Promise<MetaResponse> {
    return {
      schema_version: 2,
      data_source: 'mock',
      mock_note: SNAPSHOT.mock_note,
      generation_verified: SNAPSHOT.generation_verified,
      unverified_note: SNAPSHOT.unverified_note,
      api_key_configured: currentKeyStatus(),
      top_k_choices: SNAPSHOT.top_k_choices,
      corpora: SNAPSHOT.corpora,
      default_corpus: SNAPSHOT.default_corpus,
      // mock 模式没有服务端："我的资料库"不可用是事实，如实传 null
      user_library: null,
    };
  },

  async query(req: QueryRequest): Promise<QueryResponse> {
    await delay(RETRIEVE_MS);
    const key = pickScenario(req);
    const v = variantOf(SNAPSHOT.scenarios[key], req.top_k);
    if (v.hits.length > 0 && req.mode === 'generate') await delay(GENERATE_MS);
    return buildResponse(req, key);
  },
};

export const snapshotSource = SNAPSHOT.answers_source;
