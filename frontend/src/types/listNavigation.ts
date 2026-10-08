/**
 * 列表页与详情页之间通过 react-router 的 location.state 传递的导航状态。
 *
 * 用途有两条：
 *  1. 详情页点「返回列表」：列表按这份状态恢复分页 / 筛选 / 滚动位置，
 *     并把刚处理的那张票滚动定位出来（focusId 由详情页拼到 state 里）。
 *  2. 详情页的「上一张 / 下一张」：里面的筛选条件用来拉一份同序的完整
 *     发票 ID 列表，保证切换顺序和列表页完全一致。
 */
export interface ListRestoreState {
  page: number;
  pageSize: number;
  searchValue: string;
  statusFilter?: string;
  ownerFilter: string;
  /** [YYYY-MM-DD, YYYY-MM-DD]，没有日期筛选时为 null。dayjs 不能直接塞进
   *  location.state —— 历史栈会做结构化克隆，dayjs 对象过不去，所以存字符串。 */
  dateRange: [string, string] | null;
  scrollY: number;
}
