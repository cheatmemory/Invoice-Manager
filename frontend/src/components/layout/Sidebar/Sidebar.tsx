import { Link, useLocation } from 'react-router-dom';
import { Tooltip } from 'antd';
import {
  CheckCircleOutlined,
  ExclamationCircleOutlined,
} from '@ant-design/icons';
import styles from './Sidebar.module.css';

export interface NavItem {
  path: string;
  label: string;
}

/** 导出给 MainLayout：侧边栏在窄屏收起后，顶部那条导航要用同一份清单，
 *  免得两边各写一遍、加功能时漏改一处。 */
export const NAV_ITEMS: NavItem[] = [
  { path: '/', label: '发票列表' },
  { path: '/upload', label: '上传发票' },
];

interface SidebarProps {
  llmConfigured: boolean | null;
  onOpenLLMConfig: () => void;
}

const navItems = NAV_ITEMS;

export default function Sidebar({ llmConfigured, onOpenLLMConfig }: SidebarProps) {
  const location = useLocation();

  return (
    <aside className={styles.sidebar}>
      {/* Top Section: Logo + Navigation */}
      <div className={styles.sidebarTop}>
        {/* Logo */}
        <div className={styles.logo}>
          <div className={styles.logoMark} />
          <span className={styles.logoText}>发票管理</span>
        </div>

        {/* Navigation */}
        <nav className={styles.navigation}>
          {navItems.map((item) => {
            const isActive = location.pathname === item.path;
            return (
              <Link
                key={item.path}
                to={item.path}
                className={`${styles.navItem} ${isActive ? styles.active : ''}`}
              >
                <span className={styles.navDot} />
                <span className={styles.navLabel}>{item.label}</span>
              </Link>
            );
          })}

          {/* Settings (LLM Config) - appears as nav item */}
          <Tooltip title={llmConfigured ? 'LLM服务已配置' : 'LLM服务未配置，点击配置'} placement="right">
            <button
              className={styles.navItem}
              onClick={onOpenLLMConfig}
              style={{ border: 'none', background: 'none', width: '100%', textAlign: 'left' }}
            >
              <span className={styles.navDot} />
              <span className={styles.navLabel}>
                设置
                {llmConfigured === false && (
                  <ExclamationCircleOutlined
                    style={{ marginLeft: 8, color: 'var(--warning)', fontSize: 12 }}
                  />
                )}
                {llmConfigured === true && (
                  <CheckCircleOutlined
                    style={{ marginLeft: 8, color: 'var(--success)', fontSize: 12 }}
                  />
                )}
              </span>
            </button>
          </Tooltip>
        </nav>
      </div>

      {/* Bottom Section: User Profile */}
      <div className={styles.sidebarBottom}>
        <div className={styles.userProfile}>
          <div className={styles.userAvatar}>
            <span className={styles.userInitial}>A</span>
          </div>
          <div className={styles.userInfo}>
            <span className={styles.userName}>Anderson</span>
            <span className={styles.userRole}>管理员</span>
          </div>
        </div>
      </div>
    </aside>
  );
}
