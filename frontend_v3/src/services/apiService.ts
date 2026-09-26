/**
 * 第二阶段 A 数据源：本机 API（`api_v3.py`）。
 *
 * 页面拿到的仍然是 `types/index.ts` 里那套结构，组件一行都不用改形状。
 * 服务端只做校验 + 调 `rag_core.py` + 转 JSON；前端这里也不做任何"重算"或"补数据"。
 *
 * 失败必须分类，不能混成一句"出错了"：
 *   unreachable → 连不上本机 API（进程没起 / 端口不对 / 超时）；
 *   rejected    → 服务端明确拒绝（HTTP 4xx，附 error.code）；
 *   server      → 服务端 5xx 或返回了非 JSON。
 */
import { API_BASE } from './config';
import type {
  BatchCreateRequest, BatchJobResponse,
  DataService, MetaResponse, QueryRequest, QueryResponse,
  UserLibraryImportResponse, UserLibraryMeta, UserLibraryReindexResponse,
  UserDocImportItem,
} from '../types';

export type ApiErrorKind = 'unreachable' | 'rejected' | 'server';

export class ApiError extends Error {
  kind: ApiErrorKind;
  status: number | null;
  code: string;

  constructor(kind: ApiErrorKind, message: string, status: number | null = null, code = '') {
    super(message);
    this.name = 'ApiError';
    this.kind = kind;
    this.status = status;
    this.code = code;
  }
}

/** 检索很快；生成要等模型（本机桩也走同一条路径），给足超时 */
const RETRIEVE_TIMEOUT_MS = 30_000;
const GENERATE_TIMEOUT_MS = 120_000;

async function request<T>(path: string, init: RequestInit, timeoutMs: number): Promise<T> {
  const ac = new AbortController();
  const timer = window.setTimeout(() => ac.abort(), timeoutMs);
  let res: Response;
  try {
    res = await fetch(`${API_BASE}${path}`, { ...init, signal: ac.signal });
  } catch (e) {
    const aborted = (e as Error)?.name === 'AbortError';
    throw new ApiError(
      'unreachable',
      aborted
        ? `请求超时（${timeoutMs} ms）：${API_BASE}${path} 没有在预期时间内返回。`
        : `连接不上 ${API_BASE}${path}（连接被拒绝或地址不可达）。请确认 api_v3.py 已在本机启动。`,
    );
  } finally {
    window.clearTimeout(timer);
  }

  const text = await res.text();
  let data: unknown = null;
  try {
    data = text ? JSON.parse(text) : null;
  } catch {
    data = null;
  }

  if (!res.ok) {
    const err = (data as { error?: { code?: string; message?: string } } | null)?.error;
    const detail = err?.message ?? `HTTP ${res.status}`;
    throw new ApiError(
      res.status >= 500 ? 'server' : 'rejected',
      `${detail}${err?.code ? `（错误码 ${err.code}）` : ''}（HTTP ${res.status}）`,
      res.status,
      err?.code ?? '',
    );
  }
  if (data === null) {
    throw new ApiError('server', `本机 API 返回了非 JSON 内容（HTTP ${res.status}）。`, res.status);
  }
  return data as T;
}

export const apiService: DataService = {
  fetchMeta(): Promise<MetaResponse> {
    return request<MetaResponse>('/api/meta', { method: 'GET' }, RETRIEVE_TIMEOUT_MS);
  },

  query(req: QueryRequest): Promise<QueryResponse> {
    return request<QueryResponse>(
      '/api/query',
      {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(req),
      },
      req.mode === 'generate' ? GENERATE_TIMEOUT_MS : RETRIEVE_TIMEOUT_MS,
    );
  },

  // ---------------- 我的资料库（40 号方案 v1；失败分类与 query 相同） ----------------
  listUserDocuments(): Promise<UserLibraryMeta> {
    return request<UserLibraryMeta>('/api/user-documents', { method: 'GET' }, RETRIEVE_TIMEOUT_MS);
  },

  importUserDocuments(items: UserDocImportItem[]): Promise<UserLibraryImportResponse> {
    return request<UserLibraryImportResponse>(
      '/api/user-documents',
      {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ items }),
      },
      120_000,   // 重建索引要读全库，给足时间
    );
  },

  deleteUserDocument(id: string): Promise<{ deleted: string; library: UserLibraryMeta }> {
    return request<{ deleted: string; library: UserLibraryMeta }>(
      `/api/user-documents/${encodeURIComponent(id)}`,
      {
        method: 'DELETE',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ confirm: true }),
      },
      RETRIEVE_TIMEOUT_MS,
    );
  },

  reindexUserLibrary(): Promise<UserLibraryReindexResponse> {
    return request<UserLibraryReindexResponse>(
      '/api/user-documents/reindex',
      { method: 'POST' },
      RETRIEVE_TIMEOUT_MS,
    );
  },

  // ---------------- 批量提问（47 号方案阶段 A） ----------------
  // 页面只负责"发请求 + 照原样显示"：状态机、上限、串行、零重试全在服务端 batch_jobs.py。
  createBatchJob(req: BatchCreateRequest): Promise<BatchJobResponse> {
    return request<BatchJobResponse>(
      '/api/batch-jobs',
      {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(req),
      },
      // 创建只做校验并立刻返回（执行在后台线程），但首次提问要建 BM25 索引，给足时间
      120_000,
    );
  },

  getBatchJob(jobId: string): Promise<BatchJobResponse> {
    return request<BatchJobResponse>(
      `/api/batch-jobs/${encodeURIComponent(jobId)}`,
      { method: 'GET' },
      RETRIEVE_TIMEOUT_MS,
    );
  },

  cancelBatchJob(jobId: string): Promise<BatchJobResponse> {
    return request<BatchJobResponse>(
      `/api/batch-jobs/${encodeURIComponent(jobId)}/cancel`,
      { method: 'POST' },
      RETRIEVE_TIMEOUT_MS,
    );
  },

  /** 导出是浏览器直接下载：URL 只在这一处拼装（文件名由服务端 Content-Disposition 给） */
  batchExportUrl(jobId: string, format: 'csv' | 'docx'): string {
    return `${API_BASE}/api/batch-jobs/${encodeURIComponent(jobId)}/export?format=${format}`;
  },
};
