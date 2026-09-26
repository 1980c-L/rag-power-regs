import { useCallback, useEffect, useMemo, useState } from 'react';
import { DATA_SOURCE, service } from '../services';
import { ApiError } from '../services/apiService';
import {
  forcedQuestion,
  forcedResponse,
  forcedSettings,
  readForcedState,
} from '../services/mockService';
import type { MetaResponse, QueryResponse, Run, Settings, UiStateKey } from '../types';

/** 结果的设置与当前控件不一致时的差异项 */
export interface SettingsDrift {
  corpus: boolean;
  mode: boolean;
  top_k: boolean;
  any: boolean;
}

/** 请求没能拿到结果时的传输层失败（api 模式才有；mock 模式不会产生） */
export interface TransportFailure {
  kind: 'unreachable' | 'rejected' | 'server' | 'unknown';
  message: string;
}

const NO_DRIFT: SettingsDrift = { corpus: false, mode: false, top_k: false, any: false };
const DEFAULT_SETTINGS: Settings = { corpus_id: 'rag_learning', mode: 'generate', top_k: 3 };

function toTransportFailure(e: unknown): TransportFailure {
  if (e instanceof ApiError) return { kind: e.kind, message: e.message };
  return { kind: 'unknown', message: e instanceof Error ? e.message : String(e) };
}

export interface Workbench {
  meta: MetaResponse | null;
  /** 元数据没取到（api 模式下即"本机 API 不可达"）；与"还在加载"区分开 */
  metaError: boolean;
  settings: Settings;
  question: string;
  setQuestion: (q: string) => void;
  run: Run;
  response: QueryResponse | null;
  /** 请求在传输层就失败了：页面上必须与"模型调用失败"分开表达 */
  transport: TransportFailure | null;
  /** 强制演示的状态（?state=…）；无则为 null，走真实交互。只在 mock 数据源下可用 */
  forcedState: UiStateKey | null;
  /**
   * 30 号复审 P1：改设置不能把旧结果重新归属到新设置。
   * 这里保留旧结果，但把"结果属于哪一次请求"（response.request）与"当前控件值"（settings）
   * 的差异显式暴露给界面，由界面显示"设置已变更"并标注结果的真实归属。
   */
  drift: SettingsDrift;
  updateSettings: (patch: Partial<Settings>) => void;
  /** 资料导入/删除/重建后由资料管理面板调用：让「我的资料库」选项的出现与消失即时生效 */
  refreshMeta: () => void;
  ask: () => void;
}

export function useWorkbench(): Workbench {
  // `?state=` 是一套"本地示例数据"的演示开关：第二阶段接真实 API 后不能再用它伪造结果，
  // 因此只在 mock 数据源下生效（api 模式下忽略地址栏，页面只显示真实请求的结果）。
  const forcedState = useMemo(
    () => (DATA_SOURCE === 'mock' ? readForcedState(window.location.search) : null),
    [],
  );

  const [meta, setMeta] = useState<MetaResponse | null>(null);
  const [metaError, setMetaError] = useState(false);
  const [settings, setSettings] = useState<Settings>(() =>
    forcedState ? forcedSettings(forcedState) : DEFAULT_SETTINGS);
  const [question, setQuestion] = useState<string>(() => (forcedState ? forcedQuestion(forcedState) : ''));
  const [response, setResponse] = useState<QueryResponse | null>(() =>
    forcedState ? forcedResponse(forcedState) : null);
  const [transport, setTransport] = useState<TransportFailure | null>(null);
  const [run, setRun] = useState<Run>(() => {
    if (!forcedState) return { question: '', status: 'idle' };
    const status: Run['status'] =
      forcedState === 'initial' ? 'idle'
        : forcedState === 'loading' ? 'retrieving'
          : forcedState === 'failure' ? 'error' : 'done';
    return { question: forcedQuestion(forcedState), status };
  });

  useEffect(() => {
    let alive = true;
    service.fetchMeta(settings)
      .then((m) => { if (alive) { setMeta(m); setMetaError(false); } })
      .catch(() => { if (alive) { setMeta(null); setMetaError(true); } });
    return () => { alive = false; };
    // 挂载时先取一次；切设置时由下面那个 effect 再取
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
    let alive = true;
    service.fetchMeta(settings)
      .then((m) => { if (alive) { setMeta(m); setMetaError(false); } })
      .catch(() => { if (alive) { setMeta(null); setMetaError(true); } });
    return () => { alive = false; };
  }, [settings]);

  const updateSettings = useCallback((patch: Partial<Settings>) => {
    setSettings((prev) => ({ ...prev, ...patch }));
  }, []);

  const refreshMeta = useCallback(() => {
    service.fetchMeta(settings)
      .then((m) => { setMeta(m); setMetaError(false); })
      .catch(() => { setMeta(null); setMetaError(true); });
  }, [settings]);

  const ask = useCallback(() => {
    const q = question.trim();
    if (!q) return;
    setTransport(null);
    setRun({ question: q, status: 'retrieving' });
    const wantGenerate = settings.mode === 'generate';
    const t = window.setTimeout(() => {
      if (wantGenerate) setRun({ question: q, status: 'generating' });
    }, 300);
    service.query({ ...settings, question: q })
      .then((res) => {
        window.clearTimeout(t);
        setResponse(res);
        setRun({ question: q, status: res.status === 'error' ? 'error' : 'done' });
      })
      .catch((e: unknown) => {
        window.clearTimeout(t);
        setRun({ question: q, status: 'error' });
        // 传输层失败：保留上一次的结果（若有），但把"本次没有任何结果"说清楚
        setTransport(toTransportFailure(e));
      });
  }, [question, settings]);

  const drift = useMemo<SettingsDrift>(() => {
    if (!response) return NO_DRIFT;
    const r = response.request;
    const d = {
      corpus: r.corpus_id !== settings.corpus_id,
      mode: r.mode !== settings.mode,
      top_k: r.top_k !== settings.top_k,
    };
    return { ...d, any: d.corpus || d.mode || d.top_k };
  }, [response, settings]);

  return {
    meta, metaError, settings, question, setQuestion, run, response, transport,
    forcedState, drift, updateSettings, refreshMeta, ask,
  };
}
