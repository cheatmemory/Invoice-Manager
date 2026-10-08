import { ReactNode } from 'react';
import { Link, useLocation } from 'react-router-dom';
import { Alert, Button } from 'antd';
import { RocketOutlined } from '@ant-design/icons';
import Sidebar, { NAV_ITEMS } from './Sidebar';
import styles from './MainLayout.module.css';

interface MainLayoutProps {
  children: ReactNode;
  llmConfigured: boolean | null;
  showLlmPromo: boolean;
  onOpenLLMConfig: () => void;
  onCloseLLMPromo: () => void;
}

export default function MainLayout({
  children,
  llmConfigured,
  showLlmPromo,
  onOpenLLMConfig,
  onCloseLLMPromo,
}: MainLayoutProps) {
  const location = useLocation();

  return (
    <div className={styles.layoutContainer}>
      <Sidebar llmConfigured={llmConfigured} onOpenLLMConfig={onOpenLLMConfig} />

      {/* 窄屏用的顶部导航。侧边栏在 ≤768px 会被 CSS 收掉（它固定占 240px，
          手机上内容区只剩 54px），导航不能跟着一起消失，所以搬到顶部。
          清单从 Sidebar 那边引过来，避免两处各写一份。 */}
      <div className={styles.topNav}>
        <span className={styles.topNavLogo}>发票管理</span>
        <nav className={styles.topNavLinks}>
          {NAV_ITEMS.map((item) => (
            <Link
              key={item.path}
              to={item.path}
              className={`${styles.topNavLink} ${
                location.pathname === item.path ? styles.topNavLinkActive : ''
              }`}
            >
              {item.label}
            </Link>
          ))}
          <button
            type="button"
            className={styles.topNavLink}
            onClick={onOpenLLMConfig}
          >
            设置
          </button>
        </nav>
      </div>

      <main className={styles.mainContent}>
        <div className={styles.contentWrapper}>
          {/* LLM Configuration Promo Banner */}
          {showLlmPromo && !llmConfigured && (
            <Alert
              className={styles.llmPromoAlert}
              message="提升发票识别准确率"
              description={
                <span>
                  当前使用免费OCR预处理服务。配置LLM服务后可获得更精准的发票信息提取和智能比对功能。
                  <Button
                    type="link"
                    icon={<RocketOutlined />}
                    onClick={onOpenLLMConfig}
                    style={{ padding: '0 4px' }}
                  >
                    立即配置
                  </Button>
                </span>
              }
              type="info"
              showIcon
              closable
              onClose={onCloseLLMPromo}
            />
          )}

          {children}
        </div>

        <footer className={styles.footer}>
          发票管理系统 ©2025 by Anderson.
        </footer>
      </main>
    </div>
  );
}
