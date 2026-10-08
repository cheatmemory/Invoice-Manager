import React from 'react';
import styles from './MetricCard.module.css';

interface MetricCardProps {
  label: string;
  value: string | number;
  prefix?: string;
  /** 首次加载期间为 true：显示占位符，而不是把「还没拿到数据」画成 0。 */
  loading?: boolean;
}

const MetricCard: React.FC<MetricCardProps> = ({
  label,
  value,
  prefix = '',
  loading = false,
}) => {
  return (
    <div className={styles.metricCard}>
      <div className={styles.label}>{label}</div>
      <div className={styles.value}>
        {loading ? (
          <span className={styles.placeholder} aria-label="加载中">
            —
          </span>
        ) : (
          <>
            {prefix}
            {value}
          </>
        )}
      </div>
    </div>
  );
};

export default MetricCard;
