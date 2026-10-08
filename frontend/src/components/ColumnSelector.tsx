import { useState } from 'react';
import { Button, Checkbox, Popover, Space, Divider, message } from 'antd';
import { SettingOutlined } from '@ant-design/icons';
import type { CheckboxChangeEvent } from 'antd/es/checkbox';
import styles from './ColumnSelector.module.css';

export interface ColumnConfig {
  key: string;
  title: string;
  visible: boolean;
  fixed?: boolean; // If true, cannot be hidden
}

interface ColumnSelectorProps {
  columns: ColumnConfig[];
  onChange: (columns: ColumnConfig[]) => void;
}

function ColumnSelector({ columns, onChange }: ColumnSelectorProps) {
  const [open, setOpen] = useState(false);

  const handleChange = (key: string, e: CheckboxChangeEvent) => {
    if (!e.target.checked) {
      // 至少留一列。全关掉之后表格是个空壳，用户看到的第一反应是「页面坏了」，
      // 而不是「我把列都隐藏了」—— 想找回来还得再打开这个浮层。
      const stillVisible = columns.filter((c) => c.visible && c.key !== key).length;
      if (stillVisible === 0) {
        message.warning('至少要保留一列显示');
        return;
      }
    }

    const newColumns = columns.map((col) =>
      col.key === key ? { ...col, visible: e.target.checked } : col
    );
    onChange(newColumns);
  };

  const handleSelectAll = () => {
    const newColumns = columns.map((col) => ({ ...col, visible: true }));
    onChange(newColumns);
  };

  const handleReset = () => {
    // Reset to default visible columns (those that are fixed or in the default set)
    const defaultVisible = [
      'invoice_number',
      'issue_date',
      'buyer_name',
      'seller_name',
      'item_name',
      'total_with_tax',
      'status',
      'owner',
      'action',
    ];
    const newColumns = columns.map((col) => ({
      ...col,
      visible: col.fixed || defaultVisible.includes(col.key),
    }));
    onChange(newColumns);
  };

  const content = (
    <div className={styles.popoverContent}>
      <div className={styles.popoverHeader}>
        <Button size="small" type="link" onClick={handleSelectAll}>
          全选
        </Button>
        <Button size="small" type="link" onClick={handleReset}>
          重置
        </Button>
      </div>
      <Divider style={{ margin: '8px 0' }} />
      <Space direction="vertical" style={{ width: '100%' }}>
        {columns
          .filter((col) => col.key !== 'action')
          .map((col) => (
            <Checkbox
              key={col.key}
              checked={col.visible}
              disabled={col.fixed}
              onChange={(e) => handleChange(col.key, e)}
            >
              {col.title}
            </Checkbox>
          ))}
      </Space>
    </div>
  );

  return (
    <Popover
      content={content}
      title="选择显示列"
      trigger="click"
      open={open}
      onOpenChange={setOpen}
      placement="bottomRight"
      styles={{ body: { maxWidth: 'calc(100vw - 32px)' } }}
    >
      <Button icon={<SettingOutlined />}>列设置</Button>
    </Popover>
  );
}

export default ColumnSelector;
