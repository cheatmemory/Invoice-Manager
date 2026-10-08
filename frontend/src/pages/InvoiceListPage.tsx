import { useState, useEffect, useMemo, useRef } from 'react';
import { useNavigate, useLocation } from 'react-router-dom';
import {
  Card,
  Table,
  Tag,
  Button,
  Space,
  Select,
  Input,
  message,
  Popconfirm,
  Modal,
  Statistic,
  Row,
  Col,
  Checkbox,
  Tooltip,
} from 'antd';
import {
  EyeOutlined,
  DeleteOutlined,
  UploadOutlined,
  ReloadOutlined,
  DownloadOutlined,
  SyncOutlined,
} from '@ant-design/icons';
import type { ColumnsType } from 'antd/es/table';
import dayjs from 'dayjs';
import api, { listInvoices, deleteInvoice, batchUpdateInvoices, batchDeleteInvoices, batchReprocessInvoices, getStatistics } from '../services/api';
import type { Invoice, Statistics } from '../types/invoice';
import { InvoiceStatus } from '../types/invoice';
import type { ListRestoreState } from '../types/listNavigation';
import ResizableTitle from '../components/ResizableTitle';
import ColumnSelector from '../components/ColumnSelector';
import { useColumnSettings } from '../hooks/useColumnSettings';
import { useInvoiceStatistics } from '../hooks/useInvoiceStatistics';
import { useDebouncedValue } from '../hooks/useDebouncedValue';
import MetricCard from '../components/dashboard/MetricCard';
import ControlBar from '../components/dashboard/ControlBar';
import styles from './InvoiceListPage.module.css';
import 'react-resizable/css/styles.css';

const statusColors: Record<string, string> = {
  '已上传': 'default',
  '解析中': 'processing',
  '待处理': 'blue',
  '待审核': 'warning',
  '已确认': 'success',
  '已拒绝': 'error',
  '已报销': 'green',
  '未报销': 'orange',
};

/**
 * 从响应头里挑出后端给的真实文件名。
 * 后端用的是 `filename*=UTF-8''...` 那种 RFC 5987 写法，也可能退回普通
 * `filename="..."`；两个都没有时按格式给个兜底名。
 */
function exportFileName(headers: unknown, format: 'csv' | 'excel'): string {
  const raw = (headers as Record<string, unknown> | undefined)?.['content-disposition'];
  const text = typeof raw === 'string' ? raw : '';
  const fallback = `发票列表.${format === 'excel' ? 'xlsx' : 'csv'}`;

  const encoded = /filename\*=UTF-8''([^;]+)/i.exec(text);
  if (encoded?.[1]) {
    try {
      return decodeURIComponent(encoded[1]);
    } catch {
      return fallback;
    }
  }
  const plain = /filename="([^"]+)"/i.exec(text);
  if (plain?.[1]) {
    return plain[1];
  }
  return fallback;
}

function InvoiceListPage() {
  const navigate = useNavigate();
  const location = useLocation();
  // 从详情页点「返回列表」带回来的恢复状态。只在首次渲染时取一次：
  // 之后用户在列表里翻页/改筛选，这份旧状态不该再掺和进来。
  const restoreRef = useRef(
    (location.state as { restore?: ListRestoreState; focusId?: number } | null)
      ?.restore,
  );
  const focusRef = useRef(
    (location.state as { focusId?: number } | null)?.focusId,
  );
  /** 被定位的那一行短暂高亮（几秒后淡出）。用 state 而不是命令式加 class：
   *  antd 表格每次渲染都会按 onRow 重算 className，命令式加的 class 会被抹掉。 */
  const [focusedId, setFocusedId] = useState<number | null>(null);
  /** 表格的加载态。首次加载时表格区整体占位，翻页/改筛选时只遮罩不塌高。 */
  const [loading, setLoading] = useState(false);
  /** 上一次拉列表是不是失败过 —— 决定表格区画「重试」还是画「暂无数据」。
   *  光靠「数据为空」区分不出来：筛选后本来就该是空的。 */
  const [listFailed, setListFailed] = useState(false);
  const [invoices, setInvoices] = useState<Invoice[]>([]);
  const [total, setTotal] = useState(0);
  const [page, setPage] = useState(restoreRef.current?.page ?? 1);
  const [pageSize, setPageSize] = useState(restoreRef.current?.pageSize ?? 20);
  const [selectedRowKeys, setSelectedRowKeys] = useState<number[]>([]);
  const [statistics, setStatistics] = useState<Statistics | null>(null);
  // 正在删除的那一行 / 正在跑的批量动作。用来给对应按钮上 loading 并禁掉它，
  // 免得用户在请求飞在半路时连点好几次、或者误以为没点上。
  const [deletingId, setDeletingId] = useState<number | null>(null);
  const [busyAction, setBusyAction] = useState<string | null>(null);
  const [exporting, setExporting] = useState(false);
  // 请求序号：翻页/改筛选时连着发几个请求，先发的那个后回来就会覆盖掉
  // 后发的那份结果（页面停在旧数据上）。只认序号最大的那一次。
  const requestSeqRef = useRef(0);

  // Filters（有恢复状态时用恢复值初始化）
  const [statusFilter, setStatusFilter] = useState<string | undefined>(
    restoreRef.current?.statusFilter,
  );
  const [ownerFilter, setOwnerFilter] = useState<string>(
    restoreRef.current?.ownerFilter ?? '',
  );
  const [dateRange, setDateRange] = useState<[dayjs.Dayjs | null, dayjs.Dayjs | null] | null>(
    restoreRef.current?.dateRange
      ? [dayjs(restoreRef.current.dateRange[0]), dayjs(restoreRef.current.dateRange[1])]
      : null,
  );
  const [searchValue, setSearchValue] = useState<string>(
    restoreRef.current?.searchValue ?? '',
  );
  // 导出时是否附带发票明细行（Excel 多一个工作表，CSV 多带明细块）
  const [exportWithItems, setExportWithItems] = useState(false);

  // Column settings
  const {
    columnConfigs,
    setColumnConfigs,
    columnWidths,
    handleColumnResize,
    visibleColumns,
  } = useColumnSettings();

  /** 搜索框要「边打边出字」，请求要「停手再发」—— 两份值在这里分开。 */
  const debouncedSearch = useDebouncedValue(searchValue, 300);
  // 输入已经变了但防抖还没到点：输入框右侧转个小圈，说明「收到了，正在查」。
  const searchPending = searchValue !== debouncedSearch;

  /** 打包当前的列表状态，去详情页时带上 —— 回来时靠它恢复分页/筛选/滚动。 */
  const currentListState = (): ListRestoreState => ({
    page,
    pageSize,
    searchValue,
    statusFilter,
    ownerFilter,
    dateRange:
      dateRange?.[0] && dateRange?.[1]
        ? [dateRange[0].format('YYYY-MM-DD'), dateRange[1].format('YYYY-MM-DD')]
        : null,
    scrollY: window.scrollY,
  });

  const goDetail = (id: number) =>
    navigate(`/invoices/${id}`, { state: { listState: currentListState() } });

  /** 表头吸附的顶部偏移。桌面端没有固定顶栏（偏移 0）；≤768px 的顶部导航
   *  是 sticky 的，表头要吸在它下面，否则会叠在一起。窗口尺寸变化时重算。 */
  const [headerOffset, setHeaderOffset] = useState(0);
  useEffect(() => {
    const compute = () => {
      const nav = document.querySelector<HTMLElement>('[class*="topNav"]');
      const visible = nav && getComputedStyle(nav).display !== 'none';
      setHeaderOffset(visible ? nav.offsetHeight : 0);
    };
    compute();
    window.addEventListener('resize', compute);
    return () => window.removeEventListener('resize', compute);
  }, []);

  // 从详情页回来：数据到齐后把刚处理的那一行滚到视口中间并短暂高亮。
  useEffect(() => {
    const focus = focusRef.current;
    if (!focus) return;
    if (!invoices.some((i) => i.id === focus)) {
      // 票还在但不在恢复后的这一页（极少见：别处的并发改动），退回整页滚动位置
      if (restoreRef.current && invoices.length > 0) {
        window.scrollTo(0, restoreRef.current.scrollY);
        focusRef.current = undefined;
      }
      return;
    }
    // 等这一帧把行画出来再找节点
    const timer = window.setTimeout(() => {
      const el = document.querySelector(`[data-invoice-id="${focus}"]`);
      if (el) {
        el.scrollIntoView({ block: 'center' });
        setFocusedId(focus);
      }
      focusRef.current = undefined;
    }, 60);
    return () => window.clearTimeout(timer);
  }, [invoices]);

  // 高亮几秒后淡出，别一直亮着
  useEffect(() => {
    if (focusedId === null) return;
    const timer = window.setTimeout(() => setFocusedId(null), 2600);
    return () => window.clearTimeout(timer);
  }, [focusedId]);

  const fetchInvoices = async () => {
    const seq = ++requestSeqRef.current;
    setLoading(true);
    try {
      const params: Record<string, unknown> = { page, page_size: pageSize };
      if (statusFilter) params.status = statusFilter;
      if (ownerFilter) params.owner = ownerFilter;
      if (dateRange?.[0]) params.start_date = dateRange[0].format('YYYY-MM-DD');
      if (dateRange?.[1]) params.end_date = dateRange[1].format('YYYY-MM-DD');
      if (debouncedSearch.trim()) params.invoice_number = debouncedSearch.trim();

      const response = await listInvoices(params);
      // 回来的时候已经不是这一次请求了（用户又翻了页/改了条件），丢掉。
      if (seq !== requestSeqRef.current) return;
      setInvoices(response.items);
      setTotal(response.total);
      setListFailed(false);
    } catch (error) {
      if (seq !== requestSeqRef.current) return;
      // 提示由 axios 拦截器统一弹，这里只记个状态，供表格区画「重试」。
      setListFailed(true);
      console.error(error);
    } finally {
      if (seq === requestSeqRef.current) setLoading(false);
    }
  };

  const fetchStatistics = async () => {
    if (selectedRowKeys.length === 0) {
      setStatistics(null);
      return;
    }

    try {
      const stats = await getStatistics(selectedRowKeys);
      setStatistics(stats);
    } catch (error) {
      console.error(error);
    }
  };

  // 依赖用防抖后的那份：每敲一个字符不会重跑这个 effect。
  useEffect(() => {
    fetchInvoices();
  }, [page, pageSize, statusFilter, ownerFilter, dateRange, debouncedSearch]);

  useEffect(() => {
    fetchStatistics();
  }, [selectedRowKeys]);

  const handleStatusChange = (value: string | undefined) => {
    setStatusFilter(value);
    setPage(1);
  };

  const handleSearchChange = (value: string) => {
    setSearchValue(value);
    setPage(1);
  };

  /** 筛选/搜索/日期只要有一个非空，就给一个「清空」入口 —— 空列表最怕的是
   *  用户不知道自己其实是筛没了，而不是库里没数据。 */
  const hasActiveFilter =
    Boolean(statusFilter) ||
    Boolean(ownerFilter.trim()) ||
    Boolean(debouncedSearch.trim()) ||
    Boolean(dateRange?.[0] || dateRange?.[1]);

  const resetFilters = () => {
    setSearchValue('');
    setStatusFilter(undefined);
    setOwnerFilter('');
    setDateRange(null);
    setPage(1);
    setSelectedRowKeys([]);
  };

  const handleDateRangeChange = (range: [dayjs.Dayjs | null, dayjs.Dayjs | null] | null) => {
    setDateRange(range);
    setPage(1);
  };

  const handleOwnerChange = (value: string) => {
    setOwnerFilter(value);
    setPage(1);
  };

  const handleDelete = async (id: number) => {
    setDeletingId(id);
    try {
      await deleteInvoice(id);
      message.success('删除成功');
      fetchInvoices();
    } catch (error) {
      // 失败提示由拦截器统一弹。
      console.error(error);
    } finally {
      setDeletingId(null);
    }
  };

  const handleBatchUpdate = async (status: string) => {
    if (selectedRowKeys.length === 0) {
      message.warning('请先选择发票');
      return;
    }
    if (busyAction) return;

    setBusyAction('update');
    try {
      await batchUpdateInvoices(selectedRowKeys, status);
      message.success(`已更新 ${selectedRowKeys.length} 张发票的状态`);
      setSelectedRowKeys([]);
      fetchInvoices();
    } catch (error) {
      console.error(error);
    } finally {
      setBusyAction(null);
    }
  };

  const handleBatchDelete = () => {
    if (selectedRowKeys.length === 0) {
      message.warning('请先选择发票');
      return;
    }

    Modal.confirm({
      title: '确认批量删除',
      content: `确定要删除选中的 ${selectedRowKeys.length} 张发票吗？此操作不可恢复。`,
      okText: '确认删除',
      okType: 'danger',
      cancelText: '取消',
      onOk: async () => {
        setBusyAction('delete');
        try {
          const result = await batchDeleteInvoices(selectedRowKeys);
          message.success(result.message);
          setSelectedRowKeys([]);
          fetchInvoices();
        } catch (error) {
          console.error(error);
        } finally {
          setBusyAction(null);
        }
      },
    });
  };

  const handleBatchReprocess = () => {
    if (selectedRowKeys.length === 0) {
      message.warning('请先选择发票');
      return;
    }

    Modal.confirm({
      title: '确认重新解析',
      content: `确定要重新解析选中的 ${selectedRowKeys.length} 张发票吗？旧的解析结果将被清除。`,
      okText: '确认重新解析',
      cancelText: '取消',
      onOk: async () => {
        setBusyAction('reprocess');
        try {
          const result = await batchReprocessInvoices(selectedRowKeys);
          message.success(result.message);
          setSelectedRowKeys([]);
          fetchInvoices();
        } catch (error) {
          console.error(error);
        } finally {
          setBusyAction(null);
        }
      },
    });
  };

  const handleExport = async (format: 'csv' | 'excel') => {
    const params = new URLSearchParams();

    if (selectedRowKeys.length > 0) {
      params.append('invoice_ids', selectedRowKeys.join(','));
    }
    if (statusFilter) {
      params.append('status', statusFilter);
    }
    if (ownerFilter) {
      params.append('owner', ownerFilter);
    }
    if (dateRange?.[0]) {
      params.append('start_date', dateRange[0].format('YYYY-MM-DD'));
    }
    if (dateRange?.[1]) {
      params.append('end_date', dateRange[1].format('YYYY-MM-DD'));
    }
    // Excel 默认带第二张「发票明细」工作表；CSV 是可选的，
    // 默认不带，免得改动原有导出的行结构。
    if (format === 'excel') {
      params.append('with_items', 'true');
    } else if (exportWithItems) {
      params.append('with_items', 'true');
    }

    // axios 的 baseURL 已经是 /api，这里再写 /api 会拼成
    // /api/api/invoices/export/...（实测 404）。
    const url = `/invoices/export/${format}?${params.toString()}`;

    setExporting(true);
    try {
      // 先自己取回文件再落地成下载：
      //  · 原来用 window.open 打开这个链接，它在浏览器里算「打开新窗口」，
      //    经常被弹窗拦截器拦掉 —— 用户点了没反应，只会以为按钮坏了；
      //  · 顺带拿到了 Content-Disposition 里的真实文件名，导出文件不再叫
      //    "download"；
      //  · 还能在失败时给出反馈，而不是静默什么都不发生。
      const response = await api.get(url, { responseType: 'blob' });
      const blob = new Blob([response.data], {
        type: (response.data as Blob).type || 'application/octet-stream',
      });
      const fileName = exportFileName(response.headers, format);

      const objectUrl = URL.createObjectURL(blob);
      const link = document.createElement('a');
      link.href = objectUrl;
      link.download = fileName;
      link.style.display = 'none';
      document.body.appendChild(link);
      link.click();
      document.body.removeChild(link);
      // 延时释放：点完立刻 revoke 的话，浏览器可能还没开始读这块数据，
      // 下载会被中途掐掉。留 30 秒足够，之后不再需要它。
      window.setTimeout(() => URL.revokeObjectURL(objectUrl), 30_000);
    } finally {
      setExporting(false);
    }
  };

  // All column definitions
  const allColumnDefinitions: Record<string, ColumnsType<Invoice>[number]> = useMemo(
    () => ({
      invoice_number: {
        title: '发票号码',
        dataIndex: 'invoice_number',
        key: 'invoice_number',
        width: columnWidths.invoice_number,
        ellipsis: true,
        render: (val) => val || '-',
        onHeaderCell: () => ({
          width: columnWidths.invoice_number,
          onResize: handleColumnResize('invoice_number'),
        }),
      },
      issue_date: {
        title: '开票日期',
        dataIndex: 'issue_date',
        key: 'issue_date',
        width: columnWidths.issue_date,
        render: (val) => val || '-',
        onHeaderCell: () => ({
          width: columnWidths.issue_date,
          onResize: handleColumnResize('issue_date'),
        }),
      },
      buyer_name: {
        title: '购买方',
        dataIndex: 'buyer_name',
        key: 'buyer_name',
        width: columnWidths.buyer_name,
        ellipsis: true,
        render: (val) => val || '-',
        onHeaderCell: () => ({
          width: columnWidths.buyer_name,
          onResize: handleColumnResize('buyer_name'),
        }),
      },
      buyer_tax_id: {
        title: '购买方税号',
        dataIndex: 'buyer_tax_id',
        key: 'buyer_tax_id',
        width: columnWidths.buyer_tax_id,
        ellipsis: true,
        render: (val) => val || '-',
        onHeaderCell: () => ({
          width: columnWidths.buyer_tax_id,
          onResize: handleColumnResize('buyer_tax_id'),
        }),
      },
      seller_name: {
        title: '销售方',
        dataIndex: 'seller_name',
        key: 'seller_name',
        width: columnWidths.seller_name,
        ellipsis: true,
        render: (val) => val || '-',
        onHeaderCell: () => ({
          width: columnWidths.seller_name,
          onResize: handleColumnResize('seller_name'),
        }),
      },
      seller_tax_id: {
        title: '销售方税号',
        dataIndex: 'seller_tax_id',
        key: 'seller_tax_id',
        width: columnWidths.seller_tax_id,
        ellipsis: true,
        render: (val) => val || '-',
        onHeaderCell: () => ({
          width: columnWidths.seller_tax_id,
          onResize: handleColumnResize('seller_tax_id'),
        }),
      },
      item_name: {
        title: '项目名称',
        dataIndex: 'item_name',
        key: 'item_name',
        width: columnWidths.item_name,
        ellipsis: true,
        render: (val) => val || '-',
        onHeaderCell: () => ({
          width: columnWidths.item_name,
          onResize: handleColumnResize('item_name'),
        }),
      },
      // 规格型号 / 单位 / 数量 / 单价 四列已随列设置一并移除（见 useColumnSettings）。
      amount: {
        title: '金额(不含税)',
        dataIndex: 'amount',
        key: 'amount',
        width: columnWidths.amount,
        align: 'right',
        render: (val) => (val != null ? `¥${Number(val).toFixed(2)}` : '-'),
        onHeaderCell: () => ({
          width: columnWidths.amount,
          onResize: handleColumnResize('amount'),
        }),
      },
      tax_rate: {
        title: '税率',
        dataIndex: 'tax_rate',
        key: 'tax_rate',
        width: columnWidths.tax_rate,
        render: (val) => val || '-',
        onHeaderCell: () => ({
          width: columnWidths.tax_rate,
          onResize: handleColumnResize('tax_rate'),
        }),
      },
      tax_amount: {
        title: '税额',
        dataIndex: 'tax_amount',
        key: 'tax_amount',
        width: columnWidths.tax_amount,
        align: 'right',
        render: (val) => (val != null ? `¥${Number(val).toFixed(2)}` : '-'),
        onHeaderCell: () => ({
          width: columnWidths.tax_amount,
          onResize: handleColumnResize('tax_amount'),
        }),
      },
      total_with_tax: {
        title: '价税合计',
        dataIndex: 'total_with_tax',
        key: 'total_with_tax',
        width: columnWidths.total_with_tax,
        align: 'right',
        render: (val) => (val != null ? `¥${Number(val).toFixed(2)}` : '-'),
        onHeaderCell: () => ({
          width: columnWidths.total_with_tax,
          onResize: handleColumnResize('total_with_tax'),
        }),
      },
      status: {
        title: '状态',
        dataIndex: 'status',
        key: 'status',
        width: columnWidths.status,
        render: (status: string) => (
          <Tag color={statusColors[status] || 'default'}>{status}</Tag>
        ),
        onHeaderCell: () => ({
          width: columnWidths.status,
          onResize: handleColumnResize('status'),
        }),
      },
      owner: {
        title: '归属人',
        dataIndex: 'owner',
        key: 'owner',
        width: columnWidths.owner,
        render: (val) => val || '-',
        onHeaderCell: () => ({
          width: columnWidths.owner,
          onResize: handleColumnResize('owner'),
        }),
      },
      file_name: {
        title: '文件名',
        dataIndex: 'file_name',
        key: 'file_name',
        width: columnWidths.file_name,
        ellipsis: true,
        render: (val) => val || '-',
        onHeaderCell: () => ({
          width: columnWidths.file_name,
          onResize: handleColumnResize('file_name'),
        }),
      },
      created_at: {
        title: '创建时间',
        dataIndex: 'created_at',
        key: 'created_at',
        width: columnWidths.created_at,
        render: (val) => (val ? dayjs(val).format('YYYY-MM-DD HH:mm') : '-'),
        onHeaderCell: () => ({
          width: columnWidths.created_at,
          onResize: handleColumnResize('created_at'),
        }),
      },
      updated_at: {
        title: '更新时间',
        dataIndex: 'updated_at',
        key: 'updated_at',
        width: columnWidths.updated_at,
        render: (val) => (val ? dayjs(val).format('YYYY-MM-DD HH:mm') : '-'),
        onHeaderCell: () => ({
          width: columnWidths.updated_at,
          onResize: handleColumnResize('updated_at'),
        }),
      },
      action: {
        title: '操作',
        key: 'action',
        width: columnWidths.action,
        fixed: 'right' as const,
        render: (_, record) => (
          // size={0} 用 Space 的紧凑档：两个文字按钮紧挨着不会挤到看不见，
          // 但每个按钮本身仍是可点的一整块（比原来那个只有图标的删除按钮热区大得多）。
          <Space size={0} className={styles.rowActions}>
            <Button
              type="link"
              icon={<EyeOutlined />}
              onClick={() => goDetail(record.id)}
            >
              详情
            </Button>
            <Popconfirm
              title={
                <div>
                  <div>确定删除这张发票吗？</div>
                  <div className={styles.popconfirmHint}>删除后无法恢复。</div>
                </div>
              }
              onConfirm={() => handleDelete(record.id)}
              okText="确定删除"
              cancelText="取消"
              okButtonProps={{ danger: true, loading: deletingId === record.id }}
            >
              {/* 带文字的删除按钮：原来只有一个图标，热区就是那 24px 见方的图标，
                  既点不准也看不出是删。删除是不可逆操作，文案上也不该只给个图标。 */}
              <Button
                type="link"
                danger
                icon={<DeleteOutlined />}
                loading={deletingId === record.id}
                disabled={deletingId !== null}
              >
                删除
              </Button>
            </Popconfirm>
          </Space>
        ),
      },
    }),
    [columnWidths, handleColumnResize, navigate, deletingId, goDetail]
  );

  // Build columns based on visible column configs
  const columns: ColumnsType<Invoice> = useMemo(() => {
    return visibleColumns
      .map((config) => allColumnDefinitions[config.key])
      .filter(Boolean);
  }, [visibleColumns, allColumnDefinitions]);

  // Calculate total scroll width
  const scrollX = useMemo(() => {
    return visibleColumns.reduce(
      (sum, col) => sum + (columnWidths[col.key] || 100),
      0
    );
  }, [visibleColumns, columnWidths]);

  // Calculate statistics from current page invoices
  const pageStatistics = useInvoiceStatistics(invoices);

  // Generate total text for control bar
  const totalPages = useMemo(() => Math.max(1, Math.ceil(total / pageSize)), [total, pageSize]);
  const displayStart = total === 0 ? 0 : (page - 1) * pageSize + 1;
  const displayEnd = total === 0 ? 0 : Math.min(page * pageSize, total);

  const totalText = useMemo(() => {
    return `显示 ${displayStart}-${displayEnd} 条，共 ${total} 条`;
  }, [displayStart, displayEnd, total]);

  useEffect(() => {
    if (page > totalPages) {
      setPage(totalPages);
    }
  }, [page, totalPages]);

  // Table components with resizable header
  const tableComponents = {
    header: {
      cell: ResizableTitle,
    },
  };

  // 只有「一张数据都还没拿到」才算首屏加载 —— 翻页/筛选时保留旧数据，
  // 卡片不该跟着闪成占位符。
  const metricsLoading = loading && invoices.length === 0 && !listFailed;

  /**
   * 表格区的三种状态。antd 默认只给一行「暂无数据」，用户筛没了和库里真没钱
   * 看着一模一样；加载失败更是只飘一句 toast， toast 一消失用户就留在原地
   * 不知道该干什么。这里把三种情况各自给一个明确的出口。
   */
  const renderEmpty = () => {
    if (listFailed) {
      return (
        <div className={styles.stateBox} data-testid="list-error-state">
          <div className={styles.stateTitle}>发票列表加载失败</div>
          <div className={styles.stateDesc}>
            可能是后端服务没启动或网络断了。检查服务后可以直接重试，不用刷新整页。
          </div>
          <Button type="primary" icon={<ReloadOutlined />} onClick={fetchInvoices}>
            重新加载
          </Button>
        </div>
      );
    }

    if (hasActiveFilter) {
      return (
        <div className={styles.stateBox} data-testid="list-empty-filtered">
          <div className={styles.stateTitle}>没有符合条件的发票</div>
          <div className={styles.stateDesc}>
            当前的搜索/筛选条件太窄了，放宽这些条件再看看。
          </div>
          <Button onClick={resetFilters}>清空筛选条件</Button>
        </div>
      );
    }

    return (
      <div className={styles.stateBox} data-testid="list-empty-state">
        <div className={styles.stateTitle}>还没有任何发票</div>
        <div className={styles.stateDesc}>
          上传第一张发票后，解析结果会显示在这里。
        </div>
        <Button type="primary" icon={<UploadOutlined />} onClick={() => navigate('/upload')}>
          上传发票
        </Button>
      </div>
    );
  };

  return (
    <div className={styles.pageContainer}>
      {/* Page Header */}
      <div className={styles.pageHeader}>
        <div className={styles.headerContent}>
          <h1 className={styles.pageTitle}>发票列表</h1>
          <p className={styles.pageSubtitle}>管理和查看所有发票记录</p>
        </div>
        <div className={styles.headerActions}>
          <Button
            icon={<DownloadOutlined />}
            onClick={() => handleExport('excel')}
            className={styles.secondaryButton}
            // 导出要走后端生成文件（Excel 里还有明细工作表），慢的时候
            // 没有任何反馈会让人以为没点上。
            loading={exporting}
          >
            导出
          </Button>
          <Button
            type="primary"
            icon={<UploadOutlined />}
            onClick={() => navigate('/upload')}
            className={styles.primaryButton}
          >
            上传发票
          </Button>
        </div>
      </div>

      {/* Metric Cards */}
      <div className={styles.metricsSection}>
        <MetricCard
          label="发票数量"
          value={pageStatistics.count}
          loading={metricsLoading}
        />
        <MetricCard
          label="金额合计"
          value={pageStatistics.totalAmount.toFixed(2)}
          prefix="¥"
          loading={metricsLoading}
        />
        <MetricCard
          label="税额合计"
          value={pageStatistics.totalTax.toFixed(2)}
          prefix="¥"
          loading={metricsLoading}
        />
        <MetricCard
          label="价税合计"
          value={pageStatistics.totalWithTax.toFixed(2)}
          prefix="¥"
          loading={metricsLoading}
        />
      </div>

      {/* Selected Statistics Card */}
      {statistics && (
        <Card style={{ marginBottom: 32 }}>
          <Row gutter={24}>
            <Col span={6}>
              <Statistic title="选中数量" value={statistics.count} suffix="张" />
            </Col>
            <Col span={6}>
              <Statistic
                title="金额合计"
                value={statistics.total_amount}
                precision={2}
                prefix="¥"
              />
            </Col>
            <Col span={6}>
              <Statistic
                title="税额合计"
                value={statistics.total_tax}
                precision={2}
                prefix="¥"
              />
            </Col>
            <Col span={6}>
              <Statistic
                title="价税合计"
                value={statistics.total_with_tax}
                precision={2}
                prefix="¥"
              />
            </Col>
          </Row>
        </Card>
      )}

      {/* Control Bar */}
      <ControlBar
        searchPlaceholder="搜索发票号码..."
        searchValue={searchValue}
        onSearchChange={handleSearchChange}
        searchLoading={searchPending}
        statusFilter={statusFilter}
        onStatusChange={handleStatusChange}
        statusOptions={Object.values(InvoiceStatus).map((s) => ({ label: s, value: s }))}
        dateRange={dateRange}
        onDateRangeChange={handleDateRangeChange}
        totalText={totalText}
      />

      {/* Additional Controls */}
      <div className={styles.additionalControls}>
        <Input
          placeholder="归属人"
          style={{ width: 120 }}
          value={ownerFilter}
          onChange={(e) => handleOwnerChange(e.target.value)}
          onPressEnter={fetchInvoices}
        />

        {hasActiveFilter && (
          <Button icon={<ReloadOutlined />} onClick={resetFilters}>
            清空筛选
          </Button>
        )}

        <Button icon={<ReloadOutlined />} onClick={fetchInvoices} loading={loading}>
          刷新
        </Button>

        <Tooltip title="多带一个明细块（每张发票下面跟它的货物行）">
          <Checkbox
            checked={exportWithItems}
            onChange={(e) => setExportWithItems(e.target.checked)}
          >
            导出含明细
          </Checkbox>
        </Tooltip>
        <Button icon={<DownloadOutlined />} onClick={() => handleExport('csv')}>
          导出CSV
        </Button>

        <ColumnSelector columns={columnConfigs} onChange={setColumnConfigs} />

        {selectedRowKeys.length > 0 && (
          <>
            <Select
              placeholder="批量修改状态"
              style={{ width: 140 }}
              onChange={handleBatchUpdate}
              disabled={Boolean(busyAction)}
              options={Object.values(InvoiceStatus).map((s) => ({ label: s, value: s }))}
            />
            <Button
              icon={<SyncOutlined />}
              onClick={handleBatchReprocess}
              loading={busyAction === 'reprocess'}
              disabled={Boolean(busyAction) && busyAction !== 'reprocess'}
            >
              重新解析
            </Button>
            <Button
              danger
              icon={<DeleteOutlined />}
              onClick={handleBatchDelete}
              loading={busyAction === 'delete'}
              disabled={Boolean(busyAction) && busyAction !== 'delete'}
            >
              批量删除
            </Button>
            <span className={styles.selectedCount}>
              已选择 {selectedRowKeys.length} 项
            </span>
          </>
        )}
      </div>

      {/* Table */}
      <div className={styles.tableContainer}>
        <Table
          rowKey="id"
          loading={loading}
          columns={columns}
          dataSource={invoices}
          components={tableComponents}
          rowSelection={{
            selectedRowKeys,
            onChange: (keys) => setSelectedRowKeys(keys as number[]),
          }}
          pagination={false}
          // 表头吸附：页面向下滚时列名一直在顶上，随时知道每列是什么。
          // 桌面顶栏不存在（偏移 0），窄屏吸在 sticky 顶栏下面。
          sticky={{ offsetHeader: headerOffset }}
          scroll={{ x: scrollX }}
          className={styles.invoiceTable}
          locale={{ emptyText: renderEmpty() }}
          onRow={(record) => ({
            // 给行打上数据标记：从详情页回来时用 selector 找到它滚过去；
            // 命中时挂一个短暂高亮的 class（几秒后由定时器摘掉）。
            'data-invoice-id': record.id,
            className: record.id === focusedId ? styles.focusedRow : undefined,
          })}
        />
      </div>

      {/* Custom Pagination */}
      <div className={styles.paginationRow}>
        <div className={styles.paginationLeft}>
          <span className={styles.paginationText}>
            显示 {displayStart}-{displayEnd} 共 {total} 条
          </span>
        </div>
        <div className={styles.paginationRight}>
          <Button
            disabled={page === 1}
            onClick={() => setPage(page - 1)}
          >
            上一页
          </Button>
          <span className={styles.pageNumbers}>
            {page} / {totalPages}
          </span>
          <Button
            disabled={page >= totalPages}
            onClick={() => setPage(page + 1)}
          >
            下一页
          </Button>
          <Select
            value={pageSize}
            onChange={(val) => {
              setPageSize(val);
              setPage(1);
            }}
            options={[
              { label: '10 条/页', value: 10 },
              { label: '20 条/页', value: 20 },
              { label: '50 条/页', value: 50 },
              { label: '100 条/页', value: 100 },
            ]}
            style={{ width: 120, marginLeft: 12 }}
          />
        </div>
      </div>
    </div>
  );
}

export default InvoiceListPage;
