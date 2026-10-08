import { useState, useEffect, useRef } from 'react';
import { useParams, useNavigate, useLocation } from 'react-router-dom';
import {
  Descriptions,
  Button,
  message,
  Spin,
  Row,
  Col,
  Form,
  Input,
  InputNumber,
  Select,
  Modal,
  Tooltip,
  Space,
} from 'antd';
import {
  ArrowLeftOutlined,
  ArrowRightOutlined,
  EditOutlined,
  SaveOutlined,
  SyncOutlined,
  CheckCircleOutlined,
  CloseCircleOutlined,
  DownloadOutlined,
  ReloadOutlined,
  PlusOutlined,
  DeleteOutlined,
  ZoomInOutlined,
} from '@ant-design/icons';
import { getInvoice, getInvoiceFileUrl, updateInvoice, resolveDiff, confirmInvoice, rejectInvoice, reprocessInvoice, listInvoices } from '../services/api';
import type { InvoiceDetail, InvoiceItemInput } from '../types/invoice';
import { InvoiceStatus } from '../types/invoice';
import type { ListRestoreState } from '../types/listNavigation';
import StatusTag from '../components/StatusTag';
import ImageLightbox from '../components/ImageLightbox';
import styles from './InvoiceDetailPage.module.css';

/** 明细列的合计，用来和票面对一眼；字段缺失/空串当 0 处理。 */
function sumItems(
  items: { amount?: number | string | null; tax_amount?: number | string | null }[],
  key: 'amount' | 'tax_amount',
): number {
  return items.reduce((acc, item) => acc + (Number(item[key]) || 0), 0);
}

/**
 * 编辑态的明细行。数值列统一用字符串装 —— InputNumber 开了 stringMode 之后
 * 给的就是字符串，而且票面单价印 6 位小数（11247.863248），走一遍 float
 * 再回来会掉精度。转成提交载荷时才把空串变成 null。
 */
interface EditItem {
  item_name: string;
  specification: string;
  unit: string;
  quantity: string;
  unit_price: string;
  amount: string;
  tax_rate: string;
  tax_amount: string;
}

const BLANK_ITEM: EditItem = {
  item_name: '', specification: '', unit: '',
  quantity: '', unit_price: '', amount: '',
  tax_rate: '', tax_amount: '',
};

/** 编辑态明细 -> 提交载荷。空串 = 这一格留空（后端存 NULL）。 */
function toItemPayload(rows: EditItem[]): InvoiceItemInput[] {
  const text = (v: string) => (v.trim() ? v.trim() : null);
  return rows.map((r) => ({
    item_name: text(r.item_name),
    specification: text(r.specification),
    unit: text(r.unit),
    quantity: text(r.quantity),
    unit_price: text(r.unit_price),
    amount: text(r.amount),
    tax_rate: text(r.tax_rate),
    tax_amount: text(r.tax_amount),
  }));
}

/** 后端明细 -> 编辑态。数值原样转字符串，不四舍五入。 */
function toEditItem(item: InvoiceDetail['items'][number]): EditItem {
  const text = (v: unknown) => (v === null || v === undefined ? '' : String(v));
  return {
    item_name: text(item.item_name),
    specification: text(item.specification),
    unit: text(item.unit),
    quantity: text(item.quantity),
    unit_price: text(item.unit_price),
    amount: text(item.amount),
    tax_rate: text(item.tax_rate),
    tax_amount: text(item.tax_amount),
  };
}

const fieldLabels: Record<string, string> = {
  invoice_number: '发票号码',
  issue_date: '开票日期',
  buyer_name: '购买方名称',
  buyer_tax_id: '购买方纳税人识别号',
  seller_name: '销售方名称',
  seller_tax_id: '销售方纳税人识别号',
  item_name: '项目名称',
  total_with_tax: '价税合计',
  specification: '规格型号',
  unit: '单位',
  quantity: '数量',
  unit_price: '单价',
  amount: '金额',
  tax_rate: '税率',
  tax_amount: '税额',
};

/**
 * 必填字段 —— 定义是「不填就没法确认这张发票」。
 *
 * 这份清单与后端 `POST /invoices/{id}/confirm` 里的 `critical_fields` 逐字对应，
 * 那边校验不通过会直接 400「无法确认：必填字段缺失」。前端这里只做同一个判断，
 * 目的是把错误提前暴露、并让星号有据可依。
 *
 * 全页面只有这一份清单：展示态的星号、编辑态的表单校验、确认前的前置检查
 * 都从它派生。写成三份的话，哪天后端加了字段就会出现「编辑态没星号、
 * 点确认却报错」这类对不上的情况。
 *
 * 顺序按页面上从上到下的出现次序排（价税合计跟着金额/税额那一行），
 * 与后端那份的书写次序不同 —— 校验只看集合，不看顺序。
 */
const REQUIRED_FIELDS = [
  'invoice_number',
  'issue_date',
  'buyer_name',
  'buyer_tax_id',
  'seller_name',
  'seller_tax_id',
  'item_name',
  'total_with_tax',
] as const;

const REQUIRED_FIELD_SET: ReadonlySet<string> = new Set(REQUIRED_FIELDS);

/** 编辑态的必填校验规则。antd 见到 required: true 会自己画红星星号。 */
const requiredRule = (name: keyof typeof fieldLabels) => ({
  required: true,
  message: `请填写${fieldLabels[name]}`,
});

/**
 * 依据 REQUIRED_FIELD_SET 生成展示态的字段名。Descriptions 没有 required
 * 属性，星号得自己画 —— 判断依据跟编辑态是同一份清单。
 */
function FieldLabel({ name }: { name: string }) {
  return (
    <>
      {REQUIRED_FIELD_SET.has(name) && (
        <span className={styles.requiredMark} title="必填项">
          *
        </span>
      )}
      {fieldLabels[name]}
    </>
  );
}

/** 状态 -> 标签配色。原来只判「是不是已确认」，于是「已拒绝」也会被画成
 *  蓝色的进行中，和「待审核」长得一模一样 —— 已经是终态的东西不该看起来
 *  还在流程里。 */
const STATUS_TAG_KIND: Record<
  string,
  'pending' | 'processing' | 'success' | 'warning' | 'error'
> = {
  [InvoiceStatus.UPLOADED]: 'pending',
  [InvoiceStatus.PROCESSING]: 'processing',
  [InvoiceStatus.PENDING]: 'pending',
  [InvoiceStatus.REVIEWING]: 'warning',
  [InvoiceStatus.CONFIRMED]: 'success',
  [InvoiceStatus.REJECTED]: 'error',
  [InvoiceStatus.REIMBURSED]: 'success',
  [InvoiceStatus.NOT_REIMBURSED]: 'pending',
};

/**
 * 数值列去掉无意义的尾随零。
 * Numeric(14,4) 从库里回来就是 "1.0000"、"21316.240000"，直接摆在票面单值那行
 * 里很刺眼。只删尾随零、不做四舍五入 —— 单价 11247.863248 必须原样保留，
 * 那是票面上印的位数，抹掉就等于改了票面。
 */
function trimNumber(value: string | number): string {
  const text = String(value);
  return text.includes('.') ? text.replace(/0+$/, '').replace(/\.$/, '') : text;
}

/**
 * 已挪到「发票明细」逐行维护的四个主表字段。
 *
 * 规格型号/单位/数量/单价在票面上本来就是**逐行**的概念（一张票三行货，
 * 每行的规格和单价都不同），主表上那四个单值只是建明细表之前的遗留结构。
 * 现在明细表已经能逐行改了，这两个地方同时展示、同时可写就会有两个真相。
 * 所以从「发票信息」里撤掉，同一条数据只留一个写入口。
 *
 * 主表那份值不删：一条明细都没有时（老数据、或 OCR 没抽出明细）它是唯一的
 * 兜底展示，见明细卡片里的 `headerDetailValues`。但不从这张表单提交。
 */
const DETAIL_LEVEL_FIELDS = ['specification', 'unit', 'quantity', 'unit_price'];

/**
 * 提交前剔掉上面那四个字段。
 * 不能写成 `const { specification, ...rest } = values` 来忽略：tsconfig 开了
 * noUnusedLocals，未使用的解构变量会直接编译失败。
 */
function omitDetailFields(values: Record<string, unknown>): Record<string, unknown> {
  return Object.fromEntries(
    Object.entries(values).filter(([key]) => !DETAIL_LEVEL_FIELDS.includes(key)),
  );
}

function InvoiceDetailPage() {
  const { id } = useParams<{ id: string }>();
  const navigate = useNavigate();
  const location = useLocation();
  // 从列表页带过来的列表状态（分页/筛选/滚动位置）。直达链接没有这份状态，
  // 这时「返回列表」退化为普通回首页，上一张/下一张按无筛选的全量顺序走。
  const listState = (
    location.state as { listState?: ListRestoreState } | null
  )?.listState;
  const currentId = parseInt(id || '0', 10);
  const [loading, setLoading] = useState(true);
  /** 首次加载失败。只有它才值得拦下整个页面换成「重试」——
   *  `invoice === null` 两种成因（还没回来 / 真的没有）混在一起没法区分。 */
  const [fetchFailed, setFetchFailed] = useState(false);
  const [reprocessing, setReprocessing] = useState(false);
  /** 确认中。没有它的话「确认发票」可以连点，同一张票会被确认多次。 */
  const [confirming, setConfirming] = useState(false);
  const [saving, setSaving] = useState(false);
  const [invoice, setInvoice] = useState<InvoiceDetail | null>(null);
  const [editMode, setEditMode] = useState(false);
  const [editItems, setEditItems] = useState<EditItem[]>([]);
  const [form] = Form.useForm();
  // 跟着表单里的「金额」实时走：用户也可能顺手改主表金额，明细合计要跟它比。
  const headerAmount = Form.useWatch('amount', form);
  const [resolvingDiff, setResolvingDiff] = useState<number | null>(null);
  const [customValueModal, setCustomValueModal] = useState<{ visible: boolean; diffId: number | null; fieldName: string }>({
    visible: false,
    diffId: null,
    fieldName: '',
  });
  const [customValue, setCustomValue] = useState('');
  const [rejectModalVisible, setRejectModalVisible] = useState(false);
  const [rejectReason, setRejectReason] = useState('');
  const [rejecting, setRejecting] = useState(false);
  const [lightboxOpen, setLightboxOpen] = useState(false);
  /** 缩略图那个按钮。关掉弹层时要把焦点还给它，见 closeLightbox。 */
  const previewTriggerRef = useRef<HTMLButtonElement>(null);

  // ---- 「上一张 / 下一张」快速导航 ----------------------------------------
  // 按列表页同样的筛选和排序（创建时间倒序）拉一份完整 ID 列表，
  // 切换顺序就和用户在列表里看到的完全一致。直达链接没有列表状态，
  // 就按无筛选的全量顺序走。只取 id，page_size 100 覆盖常规数据量。
  const [navIds, setNavIds] = useState<number[] | null>(null);
  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const params: Record<string, unknown> = { page: 1, page_size: 100 };
        if (listState?.statusFilter) params.status = listState.statusFilter;
        if (listState?.ownerFilter) params.owner = listState.ownerFilter;
        if (listState?.dateRange?.[0]) params.start_date = listState.dateRange[0];
        if (listState?.dateRange?.[1]) params.end_date = listState.dateRange[1];
        if (listState?.searchValue?.trim()) {
          params.invoice_number = listState.searchValue.trim();
        }
        const response = await listInvoices(params);
        if (!cancelled) setNavIds(response.items.map((item) => item.id));
      } catch (error) {
        // 拉不到就不显示导航按钮，错误提示已由 axios 拦截器弹过
        if (!cancelled) setNavIds(null);
        console.error('Failed to load invoice navigation ids:', error);
      }
    })();
    return () => {
      cancelled = true;
    };
    // listState 由 goTo 原样透传，引用稳定，不会反复重拉
  }, [listState]);

  const navIndex = navIds?.indexOf(currentId) ?? -1;
  const prevId = navIds && navIndex > 0 ? navIds[navIndex - 1] : null;
  const nextId =
    navIds && navIndex >= 0 && navIndex < navIds.length - 1
      ? navIds[navIndex + 1]
      : null;

  /** 切到相邻发票：listState 原样透传，返回列表的恢复能力不丢。 */
  const goTo = (targetId: number) =>
    navigate(`/invoices/${targetId}`, { state: { listState } });

  /** 返回列表：把列表状态还回去，并告诉列表定位到这张票。 */
  const backToList = () =>
    navigate('/', {
      state: listState ? { restore: listState, focusId: currentId } : undefined,
    });

  /** 拉一次详情。
   *  silent = 不切整页加载态：保存/确认/裁决之后的刷新不该把用户正在看的内容
   *  换成转圈，只有首次进入才需要。
   *  失败提示统一由 axios 拦截器弹，这里只记状态，供下面渲染「重试」。 */
  const fetchInvoice = async (silent = false) => {
    if (!id) return;

    if (!silent) setLoading(true);
    try {
      const data = await getInvoice(parseInt(id));
      setInvoice(data);
      form.setFieldsValue(data);
      setFetchFailed(false);
    } catch (error) {
      // 刷新途中又失败了（用户又在点按钮）就别再弹第二次。
      if (silent && invoice) return;
      setFetchFailed(true);
      console.error(error);
    } finally {
      if (!silent) setLoading(false);
    }
  };

  useEffect(() => {
    fetchInvoice();
  }, [id]);

  /** 进编辑态时把明细拍一份快照，取消时直接丢掉即可，不碰后端。 */
  const startEdit = () => {
    setEditItems((invoice?.items ?? []).map(toEditItem));
    setEditMode(true);
  };

  const cancelEdit = () => {
    setEditItems([]);
    setEditMode(false);
    // 表单里可能留着用户改了一半的主表字段，撤销要重新拉一次
    if (invoice) form.setFieldsValue(invoice);
  };

  const patchItem = (index: number, patch: Partial<EditItem>) => {
    setEditItems((prev) => prev.map((it, i) => (i === index ? { ...it, ...patch } : it)));
  };

  const addItem = () => setEditItems((prev) => [...prev, { ...BLANK_ITEM }]);

  const removeItem = (index: number) =>
    setEditItems((prev) => prev.filter((_, i) => i !== index));

  const handleSave = async () => {
    if (!id || !invoice) return;

    setSaving(true);
    try {
      const values = await form.validateFields();
      // validateFields() 返回的是整个 store，里面还留着 setFieldsValue 灌进去的
      // 规格型号/单位/数量/单价（这四个已经没有对应的表单控件了）。剔掉再提交。
      const headerValues = omitDetailFields(values);
      // 明细一并提交（全量替换）。表里一行都没有时传空数组，语义是
      // 「用户把明细清空了」，与「本次不动明细」区分开。
      await updateInvoice(parseInt(id), { ...headerValues, items: toItemPayload(editItems) });
      message.success('保存成功');
      setEditMode(false);
      setEditItems([]);
      fetchInvoice(true);
    } catch (error) {
      // validateFields 校验不通过也抛在这个 catch 里，错误对象带 errorFields。
      // 这时 antd 已经在字段下方标红了，再弹一句「保存失败」等于把「你没填完」
      // 说成「系统出了问题」。
      if (error && typeof error === 'object' && 'errorFields' in error) {
        message.warning('还有必填字段没填，请补全后再保存');
        return;
      }
      // 其余失败的提示由 axios 拦截器统一弹。
      console.error(error);
    } finally {
      setSaving(false);
    }
  };

  const handleResolveDiff = async (diffId: number, source: 'ocr' | 'llm' | 'custom', customVal?: string) => {
    if (!id) return;
    setResolvingDiff(diffId);
    try {
      const result = await resolveDiff(parseInt(id), diffId, source, customVal);
      message.success(`${fieldLabels[result.field_name] || result.field_name} 已解决`);
      if (result.all_resolved) {
        // 说完「裁完了」就够了，不能说成「已确认」：确认是另一个动作，
        // 要用户去点右上角那个按钮，见 handleConfirmAll。
        message.info('所有差异已裁决，确认无误后请点右上角「确认发票」');
      }
      fetchInvoice(true);
    } catch (error) {
      console.error(error);
    } finally {
      setResolvingDiff(null);
    }
  };

  const handleConfirmAll = async () => {
    if (!id) return;

    if (invoice) {
      const missing = REQUIRED_FIELDS.filter((field) => {
        const value = invoice[field];
        if (value === null || value === undefined) {
          return true;
        }
        if (typeof value === 'string') {
          return value.trim().length === 0;
        }
        return false;
      });

      if (missing.length > 0) {
        // 报出具体缺哪几项。只说「请补全必填字段」的话，用户还得自己回头
        // 一个个找是哪个空了 —— 尤其明细里那些位置固定的星号不好扫。
        message.error(
          `请先补全必填字段：${missing.map((f) => fieldLabels[f]).join('、')}`,
        );
        return;
      }
    }

    setConfirming(true);
    try {
      await confirmInvoice(parseInt(id));
      message.success(invoice?.llm_result ? '发票已确认' : '发票已确认（OCR-only）');
      fetchInvoice(true);
    } catch (error) {
      // 后端的 detail（例如「无法确认：必填字段缺失」）由拦截器原样弹出来了，
      // 这里不用再说一遍。
      console.error(error);
    } finally {
      setConfirming(false);
    }
  };

  /** 拒绝这张票 —— 与「确认发票」互为反向动作，拒错了再点确认即可恢复。
   *  刻意不查必填：资料不全、跟业务对不上正是拒绝它的理由。 */
  const handleReject = async () => {
    if (!id) return;
    setRejecting(true);
    try {
      await rejectInvoice(parseInt(id), rejectReason.trim() || undefined);
      message.success('发票已拒绝');
      setRejectModalVisible(false);
      setRejectReason('');
      fetchInvoice(true);
    } catch (error) {
      console.error(error);
    } finally {
      setRejecting(false);
    }
  };

  /** 关掉原图弹层，并把焦点还给刚才那个缩略图按钮。
   *  不还的话焦点会掉到 body 上，键盘用户按完 ESC 就「失联」了 —— 再按 Tab
   *  是从页面第一个可聚焦元素重新开始，而不是接着原来的位置。 */
  const closeLightbox = () => {
    setLightboxOpen(false);
    previewTriggerRef.current?.focus();
  };

  const handleReprocess = async () => {
    if (!id) return;
    setReprocessing(true);
    try {
      await reprocessInvoice(parseInt(id));
      message.success('重新解析完成');
      await fetchInvoice(true);
    } catch (error) {
      console.error(error);
    } finally {
      setReprocessing(false);
    }
  };

  const handleCustomValueSubmit = async () => {
    if (customValueModal.diffId) {
      await handleResolveDiff(customValueModal.diffId, 'custom', customValue);
      setCustomValueModal({ visible: false, diffId: null, fieldName: '' });
      setCustomValue('');
    }
  };

  if (loading) {
    return (
      <div className={styles.loadingContainer}>
        {/* Spin 单独使用时 antd 的 tip 是无效的（会告警且不渲染），
            文案用普通文本摆在它旁边。 */}
        <Space direction="vertical" align="center" size="middle">
          <Spin size="large" />
          <span className={styles.loadingText}>正在加载发票详情…</span>
        </Space>
      </div>
    );
  }

  // 加载失败。原来只飘一句「获取发票详情失败」的 toast，toast 几秒就消失了，
  // 用户还留在原地对着半张页面发呆，连后退都没个按钮。
  if (fetchFailed) {
    return (
      <div className={styles.loadingContainer}>
        <div className={styles.stateBox} data-testid="detail-error-state">
          <div className={styles.stateTitle}>发票详情加载失败</div>
          <div className={styles.stateDesc}>
            可能是后端服务没启动、网络断了，或者这张发票已被删除。
          </div>
          <Space>
            <Button onClick={backToList}>返回列表</Button>
            <Button type="primary" icon={<ReloadOutlined />} onClick={() => fetchInvoice()}>
              重新加载
            </Button>
          </Space>
        </div>
      </div>
    );
  }

  if (!invoice) {
    return (
      <div className={styles.loadingContainer} data-testid="detail-missing-state">
        <div className={styles.stateBox}>
          <div className={styles.stateTitle}>找不到这张发票</div>
          <div className={styles.stateDesc}>
            它可能已经被删除了，或者链接里的发票编号不对。
          </div>
          <Button type="primary" onClick={backToList}>
            返回列表
          </Button>
        </div>
      </div>
    );
  }

  const hasLlm = Boolean(invoice.llm_result);
  const hasDiffs = Boolean(invoice.parsing_diffs && invoice.parsing_diffs.length > 0);
  const hasUnresolvedDiffs = Boolean(invoice.parsing_diffs && invoice.parsing_diffs.some(d => !d.resolved));

  // 已裁决字段数。注意它不是「匹配数」：resolved=1 同时包含「双源一致」和
  // 「只有一侧给出值」两种正常情形，用「匹配」描述会把后者说成异常。
  const matchCount = invoice.parsing_diffs?.filter(d => d.resolved).length || 0;
  const totalCount = invoice.parsing_diffs?.length || 0;

  // 编辑态明细的实时合计，以及和主表「金额」的对不上的提示。
  // 识别不准时最需要的就是这个：改完一眼能看出还差多少。
  const editAmountSum = sumItems(editItems, 'amount');
  const editTaxSum = sumItems(editItems, 'tax_amount');
  const headerAmountNum =
    headerAmount === undefined || headerAmount === null || headerAmount === ''
      ? null
      : Number(headerAmount);
  const amountOffBy =
    headerAmountNum !== null && editItems.length > 0 && Number.isFinite(headerAmountNum)
      ? Math.round((editAmountSum - headerAmountNum) * 100) / 100
      : null;
  const itemsMismatch = amountOffBy !== null && Math.abs(amountOffBy) > 0.02;

  // 「发票信息」里撤掉了 规格型号/单位/数量/单价（改到明细表逐行维护）。
  // 但一条明细都没有时（老数据，或 OCR 没抽出明细）它们是唯一的兜底来源，
  // 全撤掉就等于把票面上的信息藏了。所以只在明细为空时补一行出来。
  const headerDetailValues = (
    [
      ['规格型号', invoice.specification],
      ['单位', invoice.unit],
      ['数量', invoice.quantity],
      ['单价', invoice.unit_price],
    ] as Array<[string, string | number | null | undefined]>
  )
    .filter(([, value]) => value !== null && value !== undefined && value !== '')
    .map(([label, value]) => `${label} ${trimNumber(value as string | number)}`);

  const hasItems = Boolean(invoice.items && invoice.items.length > 0);

  return (
    <div className={styles.pageContainer}>
      {/* Page Header */}
      <div className={styles.pageHeader}>
        <div className={styles.headerLeft}>
          <button className={styles.backButton} onClick={backToList}>
            <ArrowLeftOutlined />
            返回列表
          </button>
          {/* 快速导航：不用回列表就能连续处理。
              边界（第一张/最后一张）置灰，直达链接没有顺序信息时不显示。 */}
          {navIds && (
            <div className={styles.navButtons} data-testid="detail-nav-buttons">
              <button
                className={styles.navButton}
                onClick={() => prevId !== null && goTo(prevId)}
                disabled={prevId === null}
                title={prevId === null ? '已经是第一张' : '上一张发票'}
              >
                <ArrowLeftOutlined />
                上一张
              </button>
              <button
                className={styles.navButton}
                onClick={() => nextId !== null && goTo(nextId)}
                disabled={nextId === null}
                title={nextId === null ? '已经是最后一张' : '下一张发票'}
              >
                下一张
                <ArrowRightOutlined />
              </button>
            </div>
          )}
          <div className={styles.headerTitle}>
            <div className={styles.invoiceNumber}>
              {invoice.invoice_number || '发票详情'}
            </div>
            <div className={styles.invoiceMetadata}>
              开票日期: {invoice.issue_date || '-'} • 归属: {invoice.owner || '-'}
            </div>
          </div>
        </div>
        <div className={styles.headerActions}>
          <button
            className={styles.rejectButton}
            onClick={() => {
              setRejectReason('');
              setRejectModalVisible(true);
            }}
            disabled={rejecting}
            title="审核不通过：这张发票不予采纳"
          >
            拒绝
          </button>
          <button
            className={styles.confirmButton}
            onClick={handleConfirmAll}
            disabled={confirming}
            aria-busy={confirming}
          >
            {confirming ? '确认中…' : '确认发票'}
          </button>
        </div>
      </div>

      {/* Content Body */}
      <div className={styles.contentBody}>
        {/* Left Panel */}
        <div className={styles.leftPanel}>
          {/* Invoice Info Card */}
          <div className={styles.card}>
            <div className={styles.cardHeader}>
              <div className={styles.cardTitle}>
                发票信息
                <StatusTag status={STATUS_TAG_KIND[invoice.status] || 'processing'}>
                  {invoice.status}
                </StatusTag>
              </div>
              <div className={styles.cardActions}>
                {editMode ? (
                  <>
                    <Button onClick={cancelEdit} disabled={saving}>
                      取消
                    </Button>
                    <Button
                      type="primary"
                      icon={<SaveOutlined />}
                      onClick={handleSave}
                      loading={saving}
                    >
                      保存
                    </Button>
                  </>
                ) : (
                  <Button icon={<EditOutlined />} onClick={startEdit}>
                    编辑
                  </Button>
                )}
              </div>
            </div>
            <div className={styles.cardBody}>
              {/* 一次把两组都点名列出来。只画星号的话，用户能看出「哪个是必填」，
                  但看不出「还有哪些是选填」—— 而选填项填不填的边界正是他关心的。 */}
              <div className={styles.formHint}>
                <div>
                  带 <span className={styles.requiredMark}>*</span> 的为必填项，缺任意一项都无法「确认发票」。
                </div>
                <div className={styles.formHintOptional}>
                  选填：金额、税额、税率、归属人、状态。规格型号/单位/数量/单价 不再在此处填写，
                  按行登记在下方「发票明细」里。
                </div>
              </div>
              {editMode ? (
                <Form form={form} layout="vertical">
                  <Row gutter={16}>
                    {/* 8 个必填项的 rules 都取自 REQUIRED_FIELDS 的同源清单，
                        规则里的字段顺序与展示态、确认前检查保持一致。 */}
                    <Col span={12}>
                      <Form.Item
                        name="invoice_number"
                        label="发票号码"
                        rules={[requiredRule('invoice_number')]}
                      >
                        <Input />
                      </Form.Item>
                    </Col>
                    <Col span={12}>
                      <Form.Item
                        name="issue_date"
                        label="开票日期"
                        rules={[requiredRule('issue_date')]}
                      >
                        <Input />
                      </Form.Item>
                    </Col>
                    <Col span={12}>
                      <Form.Item
                        name="buyer_name"
                        label="购买方名称"
                        rules={[requiredRule('buyer_name')]}
                      >
                        <Input />
                      </Form.Item>
                    </Col>
                    <Col span={12}>
                      <Form.Item
                        name="buyer_tax_id"
                        label="购买方纳税人识别号"
                        rules={[requiredRule('buyer_tax_id')]}
                      >
                        <Input />
                      </Form.Item>
                    </Col>
                    <Col span={12}>
                      <Form.Item
                        name="seller_name"
                        label="销售方名称"
                        rules={[requiredRule('seller_name')]}
                      >
                        <Input />
                      </Form.Item>
                    </Col>
                    <Col span={12}>
                      <Form.Item
                        name="seller_tax_id"
                        label="销售方纳税人识别号"
                        rules={[requiredRule('seller_tax_id')]}
                      >
                        <Input />
                      </Form.Item>
                    </Col>
                    <Col span={24}>
                      <Form.Item
                        name="item_name"
                        label="项目名称"
                        rules={[requiredRule('item_name')]}
                      >
                        <Input />
                      </Form.Item>
                    </Col>
                    <Col span={8}>
                      <Form.Item
                        name="total_with_tax"
                        label="价税合计"
                        rules={[requiredRule('total_with_tax')]}
                      >
                        <Input type="number" />
                      </Form.Item>
                    </Col>
                    <Col span={8}>
                      {/* 以下四项都是选填：确认发票时不校验它们，税额/税率
                          还能由「金额 × 税率」推出来。 */}
                      <Form.Item name="amount" label="金额">
                        <Input type="number" />
                      </Form.Item>
                    </Col>
                    <Col span={8}>
                      <Form.Item name="tax_amount" label="税额">
                        <Input type="number" />
                      </Form.Item>
                    </Col>
                    <Col span={12}>
                      <Form.Item name="status" label="状态">
                        <Select
                          options={Object.values(InvoiceStatus).map((s) => ({
                            label: s,
                            value: s,
                          }))}
                        />
                      </Form.Item>
                    </Col>
                    <Col span={12}>
                      <Form.Item name="owner" label="归属人">
                        <Input />
                      </Form.Item>
                    </Col>
                  </Row>
                </Form>
              ) : (
                <Descriptions column={2} bordered size="small">
                  {/* label 统一走 FieldLabel：星号由 REQUIRED_FIELD_SET 决定，
                      与编辑态校验、确认前检查同源。 */}
                  <Descriptions.Item label={<FieldLabel name="invoice_number" />}>
                    {invoice.invoice_number || '-'}
                  </Descriptions.Item>
                  <Descriptions.Item label={<FieldLabel name="issue_date" />}>
                    {invoice.issue_date || '-'}
                  </Descriptions.Item>
                  <Descriptions.Item label={<FieldLabel name="buyer_name" />}>
                    {invoice.buyer_name || '-'}
                  </Descriptions.Item>
                  <Descriptions.Item label={<FieldLabel name="buyer_tax_id" />}>
                    {invoice.buyer_tax_id || '-'}
                  </Descriptions.Item>
                  <Descriptions.Item label={<FieldLabel name="seller_name" />}>
                    {invoice.seller_name || '-'}
                  </Descriptions.Item>
                  <Descriptions.Item label={<FieldLabel name="seller_tax_id" />}>
                    {invoice.seller_tax_id || '-'}
                  </Descriptions.Item>
                  <Descriptions.Item label={<FieldLabel name="item_name" />} span={2}>
                    {invoice.item_name || '-'}
                  </Descriptions.Item>
                  <Descriptions.Item label={<FieldLabel name="amount" />}>
                    {invoice.amount != null ? `¥${Number(invoice.amount).toFixed(2)}` : '-'}
                  </Descriptions.Item>
                  <Descriptions.Item label={<FieldLabel name="tax_rate" />}>
                    {invoice.tax_rate || '-'}
                  </Descriptions.Item>
                  <Descriptions.Item label={<FieldLabel name="tax_amount" />}>
                    {invoice.tax_amount != null ? `¥${Number(invoice.tax_amount).toFixed(2)}` : '-'}
                  </Descriptions.Item>
                  <Descriptions.Item label={<FieldLabel name="total_with_tax" />}>
                    {invoice.total_with_tax != null ? `¥${Number(invoice.total_with_tax).toFixed(2)}` : '-'}
                  </Descriptions.Item>
                  <Descriptions.Item label="归属人">
                    {invoice.owner || '-'}
                  </Descriptions.Item>
                </Descriptions>
              )}
            </div>
          </div>

          {/* 发票明细（一张发票可含多行货物） */}
          <div className={styles.card}>
            <div className={styles.cardHeader}>
              <div className={styles.cardTitle}>
                发票明细
                {editMode ? (
                  <span style={{ marginLeft: 8, color: 'var(--muted-foreground)', fontWeight: 400 }}>
                    {editItems.length} 行
                  </span>
                ) : hasItems ? (
                  <span style={{ marginLeft: 8, color: 'var(--muted-foreground)', fontWeight: 400 }}>
                    {invoice.items.length} 行
                  </span>
                ) : null}
              </div>
            </div>
            <div className={styles.cardBody}>
              {editMode ? (
                <>
                  <table className={styles.itemTable}>
                    <thead>
                      <tr>
                        <th style={{ width: 40 }}>行号</th>
                        <th>货物或应税劳务名称</th>
                        <th style={{ width: 120 }}>规格型号</th>
                        <th style={{ width: 70 }}>单位</th>
                        <th className={styles.num} style={{ width: 96 }}>数量</th>
                        <th className={styles.num} style={{ width: 140 }}>单价</th>
                        <th className={styles.num} style={{ width: 120 }}>金额</th>
                        <th style={{ width: 76 }}>税率</th>
                        <th className={styles.num} style={{ width: 120 }}>税额</th>
                        <th style={{ width: 48 }} />
                      </tr>
                    </thead>
                    <tbody>
                      {editItems.map((item, index) => (
                        <tr key={index}>
                          <td>{index + 1}</td>
                          <td>
                            <Input
                              size="small"
                              value={item.item_name}
                              onChange={(e) => patchItem(index, { item_name: e.target.value })}
                            />
                          </td>
                          <td>
                            <Input
                              size="small"
                              value={item.specification}
                              onChange={(e) => patchItem(index, { specification: e.target.value })}
                            />
                          </td>
                          <td>
                            <Input
                              size="small"
                              value={item.unit}
                              onChange={(e) => patchItem(index, { unit: e.target.value })}
                            />
                          </td>
                          {/* stringMode：单价票面印 6 位小数，走 float 会掉精度 */}
                          <td className={styles.num}>
                            <InputNumber
                              size="small"
                              stringMode
                              controls={false}
                              className={styles.itemInput}
                              value={item.quantity}
                              onChange={(v) => patchItem(index, { quantity: v ?? '' })}
                            />
                          </td>
                          <td className={styles.num}>
                            <InputNumber
                              size="small"
                              stringMode
                              controls={false}
                              className={styles.itemInput}
                              value={item.unit_price}
                              onChange={(v) => patchItem(index, { unit_price: v ?? '' })}
                            />
                          </td>
                          <td className={styles.num}>
                            <InputNumber
                              size="small"
                              stringMode
                              controls={false}
                              className={styles.itemInput}
                              value={item.amount}
                              onChange={(v) => patchItem(index, { amount: v ?? '' })}
                            />
                          </td>
                          <td>
                            <Input
                              size="small"
                              value={item.tax_rate}
                              onChange={(e) => patchItem(index, { tax_rate: e.target.value })}
                            />
                          </td>
                          <td className={styles.num}>
                            <InputNumber
                              size="small"
                              stringMode
                              controls={false}
                              className={styles.itemInput}
                              value={item.tax_amount}
                              onChange={(v) => patchItem(index, { tax_amount: v ?? '' })}
                            />
                          </td>
                          <td>
                            <Tooltip title="删除这一行">
                              <Button
                                type="text"
                                size="small"
                                danger
                                icon={<DeleteOutlined />}
                                onClick={() => removeItem(index)}
                              />
                            </Tooltip>
                          </td>
                        </tr>
                      ))}
                      <tr className={styles.itemTotal}>
                        <td colSpan={6}>合计</td>
                        <td className={styles.num}>¥{editAmountSum.toFixed(2)}</td>
                        <td />
                        <td className={styles.num}>¥{editTaxSum.toFixed(2)}</td>
                        <td />
                      </tr>
                    </tbody>
                  </table>

                  <div className={styles.itemEditBar}>
                    <Button size="small" icon={<PlusOutlined />} onClick={addItem}>
                      增加一行
                    </Button>
                    <span className={styles.itemHint}>
                      明细行由 OCR 版面抽取，识别不准时可在此直接改；保存后以这里的值为准。
                    </span>
                  </div>

                  {itemsMismatch && (
                    <div className={styles.itemWarn}>
                      明细金额合计 ¥{editAmountSum.toFixed(2)} 与发票金额 ¥
                      {headerAmountNum!.toFixed(2)} 相差 {amountOffBy! > 0 ? '+' : ''}
                      {amountOffBy!.toFixed(2)}，保存前建议核对一下。
                    </div>
                  )}
                </>
              ) : !hasItems ? (
                <div className={styles.itemEmpty}>
                  本张发票没有解析出明细行。若是多物品发票，重新解析一次即可补齐。
                  {headerDetailValues.length > 0 && (
                    <div className={styles.itemFallback}>
                      票面单值：{headerDetailValues.join(' · ')}
                    </div>
                  )}
                </div>
              ) : (
                <table className={styles.itemTable}>
                  <thead>
                    <tr>
                      <th style={{ width: 48 }}>行号</th>
                      <th>货物或应税劳务名称</th>
                      <th style={{ width: 110 }}>规格型号</th>
                      <th style={{ width: 60 }}>单位</th>
                      <th className={styles.num} style={{ width: 80 }}>数量</th>
                      <th className={styles.num} style={{ width: 130 }}>单价</th>
                      <th className={styles.num} style={{ width: 110 }}>金额</th>
                      <th style={{ width: 70 }}>税率</th>
                      <th className={styles.num} style={{ width: 110 }}>税额</th>
                    </tr>
                  </thead>
                  <tbody>
                    {invoice.items.map((item) => (
                      <tr key={item.id}>
                        <td>{item.row_no ?? '-'}</td>
                        <td>{item.item_name || '-'}</td>
                        <td>{item.specification || '-'}</td>
                        <td>{item.unit || '-'}</td>
                        <td className={styles.num}>{item.quantity ?? '-'}</td>
                        {/* 单价照原样显示：票面印 6 位小数，四舍五入会误导对账 */}
                        <td className={styles.num}>{item.unit_price ?? '-'}</td>
                        <td className={styles.num}>
                          {item.amount != null ? `¥${Number(item.amount).toFixed(2)}` : '-'}
                        </td>
                        <td>{item.tax_rate || '-'}</td>
                        <td className={styles.num}>
                          {item.tax_amount != null ? `¥${Number(item.tax_amount).toFixed(2)}` : '-'}
                        </td>
                      </tr>
                    ))}
                    <tr className={styles.itemTotal}>
                      <td colSpan={6}>合计</td>
                      <td className={styles.num}>
                        ¥{sumItems(invoice.items, 'amount').toFixed(2)}
                      </td>
                      <td />
                      <td className={styles.num}>
                        ¥{sumItems(invoice.items, 'tax_amount').toFixed(2)}
                      </td>
                    </tr>
                  </tbody>
                </table>
              )}
            </div>
          </div>

          {/* Comparison & Review Section */}
          <div className={styles.card}>
            <div className={styles.cardHeader}>
              <div className={styles.cardTitle}>
                解析结果比对
                {!hasLlm && (
                  <StatusTag status="processing">
                    OCR-only
                  </StatusTag>
                )}
                {hasLlm && hasUnresolvedDiffs && (
                  <StatusTag status="warning">
                    {totalCount - matchCount}/{totalCount} 待确认
                  </StatusTag>
                )}
                {hasLlm && hasDiffs && !hasUnresolvedDiffs && (
                  <StatusTag status="success">
                    已完成比对
                  </StatusTag>
                )}
              </div>
              <div className={styles.cardActions}>
                <Button
                  icon={<SyncOutlined spin={reprocessing} />}
                  onClick={handleReprocess}
                  loading={reprocessing}
                  size="small"
                >
                  重新解析
                </Button>
              </div>
            </div>

            <Spin spinning={reprocessing} tip="正在重新解析...">
              {hasLlm && hasDiffs && (
                <div className={styles.comparisonHeader}>
                  <span className={styles.matchStatus}>
                    {matchCount}/{totalCount} 字段已裁决
                  </span>
                </div>
              )}

              <div className={styles.cardBody}>
              {!hasLlm && (
                <div className={styles.infoAlert}>
                  发票已通过OCR识别预处理，可直接编辑后确认。配置LLM服务可获得更精准的智能比对功能，点击"重新解析"启用双重校验。
                </div>
              )}
              {hasDiffs ? (
                <table className={styles.comparisonTable}>
                  <thead>
                    <tr>
                      <th style={{ width: '20%' }}>字段</th>
                      <th style={{ width: '30%' }}>OCR识别结果</th>
                      <th style={{ width: '30%' }}>LLM解析结果</th>
                      <th style={{ width: '20%' }}>操作</th>
                    </tr>
                  </thead>
                  <tbody>
                    {invoice.parsing_diffs?.map((diff) => {
                      // 三态而不是旧的两态（ocr_value === llm_value）。
                      // 旧写法把「LLM 没给出这一格」和「两边给出不同的值」画成
                      // 同一副样子：红底 + 两个红叉，于是一张 OCR 识别正确的
                      // 发票满屏都是「冲突」，而实际上一个冲突都没有。
                      const hasOcr = Boolean(diff.ocr_value);
                      const hasLlm = Boolean(diff.llm_value);
                      const isMatch = hasOcr && hasLlm && diff.ocr_value === diff.llm_value;
                      const isConflict = hasOcr && hasLlm && diff.ocr_value !== diff.llm_value;
                      const singleSourceLabel = hasOcr ? '仅OCR采信' : '仅LLM采信';
                      return (
                        <tr key={diff.id} className={isConflict ? styles.mismatch : styles.match}>
                          <td className={styles.fieldCell}>
                            {fieldLabels[diff.field_name] || diff.field_name}
                          </td>
                          <td>
                            <div className={styles.valueCell}>
                              {isConflict && <CloseCircleOutlined className={`${styles.statusIcon} ${styles.mismatch}`} />}
                              {isMatch && <CheckCircleOutlined className={`${styles.statusIcon} ${styles.match}`} />}
                              <span>{diff.ocr_value || '-'}</span>
                            </div>
                          </td>
                          <td>
                            <div className={styles.valueCell}>
                              {isConflict && <CloseCircleOutlined className={`${styles.statusIcon} ${styles.mismatch}`} />}
                              {isMatch && <CheckCircleOutlined className={`${styles.statusIcon} ${styles.match}`} />}
                              <span>{diff.llm_value || '-'}</span>
                            </div>
                          </td>
                          <td>
                            {!diff.resolved && isConflict && (
                              <div style={{ display: 'flex', gap: '8px' }}>
                                <Button
                                  type="link"
                                  size="small"
                                  loading={resolvingDiff === diff.id}
                                  onClick={() => handleResolveDiff(diff.id, 'ocr')}
                                >
                                  选OCR
                                </Button>
                                <Button
                                  type="link"
                                  size="small"
                                  loading={resolvingDiff === diff.id}
                                  onClick={() => handleResolveDiff(diff.id, 'llm')}
                                >
                                  选LLM
                                </Button>
                                <Button
                                  type="link"
                                  size="small"
                                  onClick={() => {
                                    setCustomValueModal({ visible: true, diffId: diff.id, fieldName: diff.field_name });
                                    setCustomValue(diff.ocr_value || diff.llm_value || '');
                                  }}
                                >
                                  自定义
                                </Button>
                              </div>
                            )}
                            {diff.resolved && isConflict && (
                              <StatusTag status="success">已解决</StatusTag>
                            )}
                            {diff.resolved && isMatch && (
                              <StatusTag status="success">双源一致</StatusTag>
                            )}
                            {diff.resolved && !isConflict && !isMatch && (
                              <StatusTag status="processing">{singleSourceLabel}</StatusTag>
                            )}
                          </td>
                        </tr>
                      );
                    })}
                  </tbody>
                </table>
              ) : (
                <div style={{ textAlign: 'center', padding: '20px', color: '#999' }}>
                  {hasLlm ? '无比对差异' : 'OCR-only 模式暂无比对数据'}
                </div>
              )}
            </div>
            </Spin>
          </div>

          {/* Custom Value Modal */}
          <Modal
            title={`自定义值 - ${fieldLabels[customValueModal.fieldName] || customValueModal.fieldName}`}
            open={customValueModal.visible}
            onOk={handleCustomValueSubmit}
            onCancel={() => {
              setCustomValueModal({ visible: false, diffId: null, fieldName: '' });
              setCustomValue('');
            }}
            okText="确定"
            cancelText="取消"
          >
            <Input
              value={customValue}
              onChange={(e) => setCustomValue(e.target.value)}
              placeholder="请输入自定义值"
            />
          </Modal>

          {/* Reject Modal */}
          <Modal
            title="拒绝这张发票"
            open={rejectModalVisible}
            onOk={handleReject}
            confirmLoading={rejecting}
            onCancel={() => {
              setRejectModalVisible(false);
              setRejectReason('');
            }}
            okText="确认拒绝"
            cancelText="取消"
            okButtonProps={{ danger: true }}
          >
            <div className={styles.rejectHint}>
              拒绝后这张发票的状态会变为「已拒绝」，表示审核不通过、不予采纳。
              它不会进入报销流程；若拒错了，随时可以再点右上角「确认发票」改回来。
            </div>
            <Input.TextArea
              rows={3}
              value={rejectReason}
              onChange={(e) => setRejectReason(e.target.value)}
              placeholder="拒绝理由（选填，会记入操作日志）"
              maxLength={500}
              showCount
            />
          </Modal>
        </div>

        {/* Right Panel - PDF Preview */}
        <div className={styles.rightPanel}>
          <div className={styles.previewCard}>
            <div className={styles.previewHeader}>
              <span className={styles.previewTitle}>原始文件</span>
              <div className={styles.previewControls}>
                <a
                  href={getInvoiceFileUrl(invoice.id)}
                  download
                  className={styles.downloadButton}
                >
                  <DownloadOutlined />
                  下载
                </a>
              </div>
            </div>
            <div className={styles.previewBody}>
              <div className={styles.previewContent}>
                {invoice.file_type === 'pdf' ? (
                  <iframe
                    src={getInvoiceFileUrl(invoice.id)}
                    className={styles.previewFrame}
                    title="PDF Preview"
                  />
                ) : (
                  /* 用 button 而不是给 img 挂 onClick：按钮天生可聚焦，
                     Enter / Space 就能打开，不用自己补 tabIndex 和键盘事件。
                     点开看到的是同一个 URL，也就是原图本身，不是缩略图的放大。 */
                  <button
                    type="button"
                    ref={previewTriggerRef}
                    className={styles.previewTrigger}
                    onClick={() => setLightboxOpen(true)}
                    aria-label="放大查看原始文件"
                    title="点击放大查看原图"
                  >
                    <img
                      src={getInvoiceFileUrl(invoice.id)}
                      alt={`发票 ${invoice.invoice_number || ''} 原图`}
                      className={styles.previewImage}
                    />
                    <span className={styles.zoomHint} aria-hidden="true">
                      <ZoomInOutlined />
                      点击放大
                    </span>
                  </button>
                )}
              </div>
            </div>
          </div>
        </div>

        {/* 原图放大弹层。挂在 body 上（见组件注释），放这儿只是为了挨着它的触发源 */}
        <ImageLightbox
          open={lightboxOpen}
          src={getInvoiceFileUrl(invoice.id)}
          alt={`发票 ${invoice.invoice_number || ''} 原图`}
          onClose={closeLightbox}
        />
      </div>
    </div>
  );
}

export default InvoiceDetailPage;
