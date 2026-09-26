import { useCallback, useEffect, useRef, useState } from 'react';
import { service } from '../services';
import { ApiError } from '../services/apiService';
import type { UserLibraryMeta } from '../types';

/**
 * 「我的资料库」资料管理（40 号方案 v1）。
 *
 * 边界（不许说谎的地方）：
 *  - 仅 api 模式渲染（mock 模式没有服务端，不假装能用）；
 *  - 只支持 .txt / .md 上传与粘贴文本；PDF / Word / 网页抓取明确"不支持"；
 *  - 本库不参与评测：不显示 Hit@K / MRR，只显示来源 / chunk / 字符这类事实；
 *  - 删除必须二次确认（window.confirm + 服务端 confirm=true 双保险）。
 */

/** 文件 → base64（保留原始字节，编码探测交给服务端：GBK 文件也能正确导入） */
function fileToBase64(file: File): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => {
      const buf = reader.result as ArrayBuffer;
      const bytes = new Uint8Array(buf);
      let bin = '';
      const CHUNK = 0x8000;
      for (let i = 0; i < bytes.length; i += CHUNK) {
        bin += String.fromCharCode(...bytes.subarray(i, i + CHUNK));
      }
      resolve(btoa(bin));
    };
    reader.onerror = () => reject(reader.error ?? new Error('文件读取失败'));
    reader.readAsArrayBuffer(file);
  });
}

interface Feedback {
  kind: 'ok' | 'error';
  text: string;
}

export function UserLibraryPanel({ meta, locked, onChanged }:
{ meta: UserLibraryMeta | null; locked: boolean; onChanged?: () => void }) {
  const [lib, setLib] = useState<UserLibraryMeta | null>(meta);
  const [busy, setBusy] = useState(false);
  const [feedback, setFeedback] = useState<Feedback | null>(null);
  const [title, setTitle] = useState('');
  const [sourceName, setSourceName] = useState('');
  const [sourceUrl, setSourceUrl] = useState('');
  const [text, setText] = useState('');
  const fileRef = useRef<HTMLInputElement>(null);

  useEffect(() => { setLib(meta); }, [meta]);

  const run = useCallback(async (action: () => Promise<UserLibraryMeta>, okText: string) => {
    setBusy(true);
    setFeedback(null);
    try {
      setLib(await action());
      setFeedback({ kind: 'ok', text: okText });
      onChanged?.();   // 42 号窄修：库变化后刷新 meta，让知识库选项即时出现/消失
    } catch (e) {
      const msg = e instanceof ApiError ? e.message : (e instanceof Error ? e.message : String(e));
      setFeedback({ kind: 'error', text: msg });
    } finally {
      setBusy(false);
    }
  }, [onChanged]);

  const importPaste = useCallback(() => {
    if (!text.trim() || !title.trim() || !service.importUserDocuments) return;
    setBusy(true);
    setFeedback(null);
    void (async () => {
      try {
        const out = await service.importUserDocuments!([{
          kind: 'paste', text, title: title.trim(),
          source_name: sourceName.trim(), source_url: sourceUrl.trim(),
        }]);
        setLib(out.library);
        const r = out.results[0];
        if (r.status === 'rejected') throw new ApiError('rejected', r.reason ?? '导入被拒绝');
        if (r.status === 'duplicate') {
          // 42 号窄修：重复必须有清晰反馈，不能显示成"已导入"
          setFeedback({ kind: 'ok', text: '该内容已在库中（SHA-256 相同），未重复导入。' });
          return;
        }
        setText(''); setTitle(''); setSourceUrl('');
        setFeedback({ kind: 'ok', text: '已导入。' });
        onChanged?.();   // 42 号窄修：导入成功后刷新 meta，让「我的资料库」选项即时出现
      } catch (e) {
        const msg = e instanceof ApiError ? e.message : (e instanceof Error ? e.message : String(e));
        setFeedback({ kind: 'error', text: msg });
      } finally {
        setBusy(false);
      }
    })();
  }, [text, title, sourceName, sourceUrl]);

  const importFiles = useCallback(async (files: FileList | null) => {
    if (!files || !files.length || !service.importUserDocuments) return;
    const list = Array.from(files);
    setBusy(true);
    setFeedback(null);
    try {
      const items = [];
      for (const f of list) {
        items.push({
          kind: 'file_b64' as const,
          data_b64: await fileToBase64(f),
          original_filename: f.name,
        });
      }
      const out = await service.importUserDocuments(items);
      setLib(out.library);
      const imported = out.results.filter((r) => r.status === 'imported').length;
      const dup = out.results.filter((r) => r.status === 'duplicate').length;
      const rejected = out.results.filter((r) => r.status === 'rejected');
      const parts = [`导入 ${imported} 份`];
      if (dup) parts.push(`重复跳过 ${dup} 份`);
      if (rejected.length) {
        parts.push(`拒绝 ${rejected.length} 份：${rejected.map((r) => r.reason).join('；')}`);
      }
      setFeedback({ kind: rejected.length ? 'error' : 'ok', text: parts.join('；') + '。' });
      onChanged?.();
    } catch (e) {
      const msg = e instanceof ApiError ? e.message : (e instanceof Error ? e.message : String(e));
      setFeedback({ kind: 'error', text: msg });
    } finally {
      setBusy(false);
      if (fileRef.current) fileRef.current.value = '';
    }
  }, []);

  const removeDoc = useCallback((id: string, docTitle: string) => {
    if (!service.deleteUserDocument) return;
    // 二次确认（服务端还要 confirm=true，双保险）
    if (!window.confirm(`确定删除「${docTitle}」吗？删除后不可恢复。`)) return;
    void run(async () => (await service.deleteUserDocument!(id)).library, `已删除「${docTitle}」。`);
  }, [run]);

  const reindex = useCallback(() => {
    if (!service.reindexUserLibrary) return;
    void run(async () => (await service.reindexUserLibrary!()).library, '已按现有切分重建索引。');
  }, [run]);

  return (
    <section className="card aside-card" id="user-library-panel">
      <h2 className="aside-card__title">资料管理（我的资料库）</h2>
      {!lib && (
        <p className="muted small">连接不上本机 API，资料管理不可用。</p>
      )}
      {lib && (
        <>
          <p className="muted small">
            来源 <strong>{lib.source_count}</strong> 份 · chunk <strong>{lib.chunk_count}</strong> 个 ·{' '}
            {lib.total_chars} 字符
          </p>
          <p className="muted small">
            本库是<strong>个人资料</strong>，与内置学习库分开存放；不参与评测，
            <strong>不继承</strong>内置库的 Hit@K / MRR 指标。
          </p>

          {lib.documents.length > 0 && (
            <ul className="userlib-list">
              {lib.documents.map((d) => (
                <li key={d.id} className="userlib-item">
                  <div className="userlib-item__main">
                    <span className="userlib-item__title" title={d.id}>{d.title}</span>
                    <span className="muted small">
                      {d.chars} 字符 · 导入于 {d.imported_at}
                      {d.original_filename ? ` · ${d.original_filename}` : ''}
                      {d.source_name ? ` · 来源：${d.source_name}` : ''}
                    </span>
                  </div>
                  <button
                    type="button"
                    className="btn userlib-del"
                    disabled={busy || locked}
                    onClick={() => removeDoc(d.id, d.title)}
                  >
                    删除
                  </button>
                </li>
              ))}
            </ul>
          )}
          {lib.documents.length === 0 && (
            <p className="muted small">还没有资料。粘贴文本或上传文件后即可在这里提问。</p>
          )}

          <div className="field">
            <span className="field__label">粘贴文本（标题必填）</span>
            <textarea
              className="userlib-textarea"
              rows={4}
              placeholder="粘贴要入库的正文…"
              value={text}
              disabled={busy || locked}
              onChange={(e) => setText(e.target.value)}
            />
            <input
              className="userlib-input"
              placeholder="标题（必填）"
              value={title}
              disabled={busy || locked}
              onChange={(e) => setTitle(e.target.value)}
            />
            <div className="userlib-row">
              <input
                className="userlib-input"
                placeholder="来源名称（可选）"
                value={sourceName}
                disabled={busy || locked}
                onChange={(e) => setSourceName(e.target.value)}
              />
              <input
                className="userlib-input"
                placeholder="来源链接（可选，http(s)://）"
                value={sourceUrl}
                disabled={busy || locked}
                onChange={(e) => setSourceUrl(e.target.value)}
              />
            </div>
            <button
              type="button"
              className="btn btn--primary userlib-btn"
              disabled={busy || locked || !text.trim() || !title.trim()}
              onClick={importPaste}
            >
              导入粘贴文本
            </button>
          </div>

          <div className="field">
            <span className="field__label">
              上传文件（{lib.limits.allowed_suffixes.join(' / ')}，单份 ≤ 2 MB，一次 ≤ 10 份）
            </span>
            <input
              ref={fileRef}
              type="file"
              className="userlib-file"
              accept=".txt,.md"
              multiple
              disabled={busy || locked}
              onChange={(e) => void importFiles(e.target.files)}
            />
          </div>

          <div className="userlib-row">
            <button type="button" className="btn" disabled={busy || locked} onClick={reindex}>
              重建索引
            </button>
          </div>

          {feedback && (
            <p className={feedback.kind === 'ok' ? 'userlib-msg userlib-msg--ok' : 'userlib-msg userlib-msg--err'}>
              {feedback.text}
            </p>
          )}
        </>
      )}
    </section>
  );
}
