import axios from 'axios';
import { message } from 'antd';
import type {
  Invoice,
  InvoiceDetail,
  InvoiceUpdatePayload,
  InvoiceListResponse,
  Statistics,
  UploadResponse,
  LLMStatusResponse,
  LLMConfigRequest,
  LLMConfigResponse,
  LLMTestResponse,
  ModelsResponse,
} from '../types/invoice';

const api = axios.create({
  baseURL: '/api',
  headers: {
    'Content-Type': 'application/json',
  },
});

/**
 * 一次请求失败的原因，写成可以直接给用户看的一句话。
 *
 * 后端多数接口会带 `detail`（例如「无法确认：必填字段缺失」），那比前端
 * 硬编码的「操作失败」有用得多 —— 用户能知道错在哪。所以优先原样透出，
 * 只有后端什么都没给时才按状态码兜底。
 */
export function describeApiError(error: unknown): string {
  const err = error as {
    response?: { status?: number; data?: { detail?: unknown } };
  };
  const raw = err?.response?.data?.detail;
  if (typeof raw === 'string' && raw.trim()) {
    return raw.trim();
  }
  if (raw && typeof raw === 'object') {
    try {
      const serialized = JSON.stringify(raw);
      if (serialized && serialized !== '{}') {
        return serialized;
      }
    } catch {
      // 兜底分支，走到下面按状态码处理
    }
  }

  const status = err?.response?.status;
  // 没有 response 说明请求压根没到服务端：服务没起、断网、被代理拒了。
  if (status === undefined || status === 0) {
    return '网络连接失败，请检查后端服务是否已启动';
  }
  if (status === 404) {
    return '请求的资源不存在（404）';
  }
  if (status >= 500) {
    return `服务器出错（${status}），请稍后重试`;
  }
  return `请求失败（${status}）`;
}

/**
 * 失败提示统一在这里弹一次。
 *
 * 之前每个调用点都自己写 try/catch + `message.error('XX失败')`：同一类错误
 * 在不同页面有两种说法，而且后端写清楚的原因被一句笼统的话盖掉，还容易
 * 两个地方各弹一次。现在规则只有一份。
 */
api.interceptors.response.use(
  (response) => response,
  (error: unknown) => {
    // 主动取消（切筛选条件时丢弃排队中的旧请求）不是错误，别去打扰用户。
    if (axios.isCancel(error)) {
      return Promise.reject(error);
    }
    message.error(describeApiError(error));
    return Promise.reject(error);
  },
);

// Health check
export const healthCheck = async () => {
  const response = await api.get('/health');
  return response.data;
};

// Upload invoices
export const uploadInvoices = async (files: File[]): Promise<UploadResponse[]> => {
  const formData = new FormData();
  files.forEach((file) => {
    formData.append('files', file);
  });

  const response = await api.post('/invoices/upload', formData, {
    headers: {
      'Content-Type': 'multipart/form-data',
    },
  });
  return response.data;
};

// List invoices
export interface ListParams {
  page?: number;
  page_size?: number;
  status?: string;
  owner?: string;
  start_date?: string;
  end_date?: string;
}

export const listInvoices = async (params: ListParams = {}): Promise<InvoiceListResponse> => {
  const response = await api.get('/invoices', { params });
  return response.data;
};

// Get invoice detail
export const getInvoice = async (id: number): Promise<InvoiceDetail> => {
  const response = await api.get(`/invoices/${id}`);
  return response.data;
};

// Get invoice file URL
export const getInvoiceFileUrl = (id: number): string => {
  return `/api/invoices/${id}/file`;
};

// Update invoice（含明细行：data.items 缺省则不动明细）
export const updateInvoice = async (
  id: number,
  data: InvoiceUpdatePayload
): Promise<Invoice> => {
  const response = await api.put(`/invoices/${id}`, data);
  return response.data;
};

// Batch update
export const batchUpdateInvoices = async (
  invoiceIds: number[],
  status?: string,
  owner?: string
): Promise<{ message: string; updated_count: number }> => {
  const response = await api.post('/invoices/batch-update', {
    invoice_ids: invoiceIds,
    status,
    owner,
  });
  return response.data;
};

// Delete invoice
export const deleteInvoice = async (id: number): Promise<void> => {
  await api.delete(`/invoices/${id}`);
};

// Batch delete invoices
export const batchDeleteInvoices = async (
  invoiceIds: number[]
): Promise<{ message: string; deleted_count: number }> => {
  const response = await api.post('/invoices/batch-delete', {
    invoice_ids: invoiceIds,
  });
  return response.data;
};

// Get statistics
export const getStatistics = async (
  invoiceIds?: number[],
  status?: string,
  owner?: string
): Promise<Statistics> => {
  const params: Record<string, string> = {};
  if (invoiceIds && invoiceIds.length > 0) {
    params.invoice_ids = invoiceIds.join(',');
  }
  if (status) {
    params.status = status;
  }
  if (owner) {
    params.owner = owner;
  }

  const response = await api.get('/invoices/statistics', { params });
  return response.data;
};

// Resolve parsing diff
export const resolveDiff = async (
  invoiceId: number,
  diffId: number,
  source: 'ocr' | 'llm' | 'custom',
  customValue?: string
): Promise<{ message: string; field_name: string; final_value: string; all_resolved: boolean }> => {
  const response = await api.post(`/invoices/${invoiceId}/diffs/${diffId}/resolve`, {
    source,
    custom_value: customValue,
  });
  return response.data;
};

// Confirm invoice
export const confirmInvoice = async (
  invoiceId: number
): Promise<{ message: string; resolved_count: number }> => {
  const response = await api.post(`/invoices/${invoiceId}/confirm`);
  return response.data;
};

// Reject invoice（人工审核不通过）。理由选填。
export const rejectInvoice = async (
  invoiceId: number,
  reason?: string
): Promise<{ message: string; status: string; reason: string | null }> => {
  const response = await api.post(`/invoices/${invoiceId}/reject`, { reason });
  return response.data;
};

// Re-process invoice (run OCR/LLM again)
export const reprocessInvoice = async (
  invoiceId: number
): Promise<{ message: string; invoice_id: number }> => {
  const response = await api.post(`/invoices/${invoiceId}/process`);
  return response.data;
};

// Batch reprocess invoices (clear old results and re-run OCR/LLM)
export const batchReprocessInvoices = async (
  invoiceIds: number[]
): Promise<{ message: string; count: number }> => {
  const response = await api.post('/invoices/batch-reprocess', {
    invoice_ids: invoiceIds,
  });
  return response.data;
};

// LLM Configuration APIs

// Get LLM status
export const getLLMStatus = async (): Promise<LLMStatusResponse> => {
  const response = await api.get('/settings/llm/status');
  return response.data;
};

// Configure LLM provider
export const configureLLM = async (
  config: LLMConfigRequest,
  configToken?: string
): Promise<LLMConfigResponse> => {
  const response = await api.post('/settings/llm/configure', config, {
    headers: configToken ? { 'X-LLM-Config-Token': configToken } : undefined,
  });
  return response.data;
};

// Test LLM connection
export const testLLMConnection = async (): Promise<LLMTestResponse> => {
  const response = await api.post('/settings/llm/test');
  return response.data;
};

// Test LLM config before saving (uses actual provider client library)
export const testLLMConfig = async (
  config: { provider: string; api_key: string; model?: string; base_url?: string }
): Promise<{ success: boolean; message: string; response_time_ms?: number }> => {
  const response = await api.post('/settings/llm/test-config', config);
  return response.data;
};

// Get available models for a provider
export const getAvailableModels = async (
  provider?: string,
  visionOnly: boolean = false
): Promise<ModelsResponse> => {
  const params: Record<string, string | boolean> = {};
  if (provider) {
    params.provider = provider;
  }
  if (visionOnly) {
    params.vision_only = true;
  }
  const response = await api.get('/settings/models', { params });
  return response.data;
};

export default api;
