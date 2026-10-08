import React from 'react';
import { Input, Select, DatePicker, Spin } from 'antd';
import type { RangePickerProps } from 'antd/es/date-picker';
import styles from './ControlBar.module.css';

const { RangePicker } = DatePicker;

interface ControlBarProps {
  searchPlaceholder?: string;
  searchValue?: string;
  onSearchChange?: (value: string) => void;
  /** 防抖还没到点/请求还在飞：在搜索框右侧转个小圈，让用户知道系统收到了。
   *  没有这个提示的话，慢一点的查询会让人以为卡住了。 */
  searchLoading?: boolean;
  statusFilter?: string;
  onStatusChange?: (value: string | undefined) => void;
  statusOptions?: Array<{ label: string; value: string }>;
  dateRange?: [any, any] | null;
  onDateRangeChange?: RangePickerProps['onChange'];
  totalText?: string;
}

const ControlBar: React.FC<ControlBarProps> = ({
  searchPlaceholder = '搜索...',
  searchValue,
  onSearchChange,
  searchLoading = false,
  statusFilter,
  onStatusChange,
  statusOptions = [],
  dateRange,
  onDateRangeChange,
  totalText = '',
}) => {
  return (
    <div className={styles.controlBar}>
      <div className={styles.leftSection}>
        {/* aria-busy 是给读屏软件的；转圈那个圈只能靠眼睛看见。 */}
        <Input
          placeholder={searchPlaceholder}
          value={searchValue}
          onChange={(e) => onSearchChange?.(e.target.value)}
          className={styles.searchInput}
          allowClear
          suffix={searchLoading ? <Spin size="small" /> : null}
          aria-busy={searchLoading}
        />
        <Select
          placeholder="全部状态"
          value={statusFilter}
          onChange={onStatusChange}
          options={statusOptions}
          allowClear
          className={styles.statusFilter}
        />
        <RangePicker
          value={dateRange}
          onChange={onDateRangeChange}
          className={styles.dateRange}
        />
      </div>
      <div className={styles.rightSection}>
        {totalText && <span className={styles.totalText}>{totalText}</span>}
      </div>
    </div>
  );
};

export default ControlBar;
