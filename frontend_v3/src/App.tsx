import { useMemo } from 'react';
import { deriveSteps } from './app/derive';
import { useWorkbench } from './app/useWorkbench';
import { AnswerCard } from './components/AnswerCard';
import { BatchPanel } from './components/BatchPanel';
import { EvidenceList } from './components/EvidenceList';
import { QuestionCard } from './components/QuestionCard';
import { ReferenceCard } from './components/ReferenceCard';
import { SettingsPanel } from './components/SettingsPanel';
import { StateNotice } from './components/StateNotice';
import { StepBar } from './components/StepBar';
import { TopBar } from './components/TopBar';
import { DATA_SOURCE } from './services';
import type { GenerateMode, MetaResponse } from './types';

const MODE_TEXT: Record<GenerateMode, string> = {
  retrieve_only: '仅检索',
  generate: '检索并生成',
};

function corpusName(meta: MetaResponse | null, id: string): string {
  if (!meta) return id;
  return meta.corpora.find((c) => c.corpus_id === id)?.name ?? id;
}

export function App() {
  const wb = useWorkbench();
  const steps = useMemo(() => deriveSteps(wb.run, wb.response), [wb.run, wb.response]);
  const currentCorpus = corpusName(wb.meta, wb.settings.corpus_id);
  const busy = wb.run.status === 'retrieving' || wb.run.status === 'generating';

  // 结果的真实归属：始终读 response.request，绝不读当前控件值
  const resReq = wb.response?.request ?? null;
  const driftText = wb.drift.any
    ? [
      wb.drift.corpus ? `知识库 → ${currentCorpus}` : '',
      wb.drift.mode ? `生成模式 → ${MODE_TEXT[wb.settings.mode]}` : '',
      wb.drift.top_k ? `Top-K → ${wb.settings.top_k}` : '',
    ].filter(Boolean).join('、')
    : '';

  return (
    <div className="page" data-datasource={DATA_SOURCE} data-forced-state={wb.forcedState ?? ''}>
      <TopBar meta={wb.meta} corpusName={currentCorpus} metaError={wb.metaError} />
      <div className="layout">
        <main className="main-col">
          <StepBar view={steps} />
          <QuestionCard
            question={wb.question}
            onQuestion={wb.setQuestion}
            onAsk={wb.ask}
            settings={wb.settings}
            meta={wb.meta}
            busy={busy}
          />
          {driftText && (
            <p className="notice notice--warn" id="stale-notice">
              <strong>右侧设置已变更</strong>（{driftText}），下面显示的仍是<strong>上一次运行</strong>的结果；
              请重新点击「开始检索」。结果归属以「本次结果使用的设置」为准。
            </p>
          )}
          {resReq && (
            <p className="result-scope" id="result-scope">
              本次结果使用的设置：知识库 <strong>{corpusName(wb.meta, resReq.corpus_id)}</strong> · 生成模式{' '}
              <strong>{MODE_TEXT[resReq.mode]}</strong> · Top-K <strong>{resReq.top_k}</strong>
              {wb.drift.any && <span className="stale-flag">（与右侧当前设置不一致）</span>}
            </p>
          )}
          {wb.transport && (
            <p className="notice notice--error" id="transport-notice">
              <strong>
                {wb.transport.kind === 'unreachable'
                  ? '连接不上本机 API'
                  : wb.transport.kind === 'rejected'
                    ? '本机 API 拒绝了本次请求'
                    : '本机 API 返回了错误'}
              </strong>
              ：{wb.transport.message}
              {' '}
              本次请求没有拿到任何结果
              {wb.response ? '；下面显示的仍是上一次运行的结果，没有被当成这次的。' : '。'}
            </p>
          )}
          <StateNotice run={wb.run} response={wb.response} />
          <div className="cards-row" id="cards-row">
            <AnswerCard run={wb.run} response={wb.response} meta={wb.meta} />
            <ReferenceCard run={wb.run} response={wb.response} />
          </div>
          <EvidenceList run={wb.run} response={wb.response} />
          {/* 批量提问：独立入口（47 号方案阶段 A）。它读同一份 meta，但自己一套控件，
              不会改写单题结果，也不参与单题结果归属（result-scope）的判定。 */}
          <BatchPanel meta={wb.meta} locked={busy} />
        </main>
        <SettingsPanel
          meta={wb.meta}
          settings={wb.settings}
          onChange={wb.updateSettings}
          locked={busy}
          onMetaRefresh={wb.refreshMeta}
        />
      </div>
    </div>
  );
}
