import { useEffect, useRef } from 'react';
import { createPortal } from 'react-dom';
import { CloseOutlined, ExportOutlined } from '@ant-design/icons';
import styles from './ImageLightbox.module.css';

interface ImageLightboxProps {
  /** 是否展示。由调用方持有状态，本组件不自己开关。 */
  open: boolean;
  /** 原图地址。缩略图和它共用同一个 URL，所以「放大」看到的就是原图本身。 */
  src: string;
  /** 无障碍名称，同时作为 alt。 */
  alt: string;
  onClose: () => void;
}

/**
 * 原图放大弹层（lightbox）。
 *
 * 关闭方式给了三种，覆盖鼠标 / 触屏 / 键盘：点遮罩空白、按 ESC、点右上角关闭。
 * 点图片本身**不关**——放大看细节时手一抖就退出是很烦的，所以只认遮罩层，
 * 判据是 `e.target === e.currentTarget`（冒泡到遮罩的那些点击里，只有真的点在
 * 遮罩上才会相等）。
 *
 * 渲染走 createPortal 挂到 document.body：这是为了让 `position: fixed` 相对
 * 视口定位。一旦祖先元素有 transform/filter/will-change，fixed 就会改以那个
 * 祖先为参照系，「全屏遮罩」会缩在卡片里。挂在 body 下就绕开了这层不确定性，
 * 也不用去和页面上任何层叠上下文比 z-index。
 */
function ImageLightbox({ open, src, alt, onClose }: ImageLightboxProps) {
  const overlayRef = useRef<HTMLDivElement>(null);
  const closeButtonRef = useRef<HTMLButtonElement>(null);

  // ESC 关闭 + 把 Tab 焦点圈在弹层里。
  // 不做焦点圈的话，按 Tab 会把焦点送到遮罩底下那些看不见的按钮上 —— 视觉上
  // 弹层开着，键盘却已经跑到后面的表单里去了。
  useEffect(() => {
    if (!open) return;

    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') {
        event.stopPropagation();
        onClose();
        return;
      }
      if (event.key !== 'Tab') return;

      const focusables = overlayRef.current?.querySelectorAll<HTMLElement>(
        '[data-lightbox-focusable]',
      );
      if (!focusables || focusables.length === 0) return;

      const first = focusables[0];
      const last = focusables[focusables.length - 1];
      if (event.shiftKey && document.activeElement === first) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault();
        first.focus();
      }
    };

    document.addEventListener('keydown', onKeyDown);
    return () => document.removeEventListener('keydown', onKeyDown);
  }, [open, onClose]);

  // 打开时把焦点交给「关闭」，并锁住背景滚动。
  // 锁滚动是必须的：弹层开着还能滚页面的话，关掉之后用户会落在一个陌生的位置，
  // 而且右侧那个跟随视口的预览卡会在遮罩底下动来动去。
  useEffect(() => {
    if (!open) return;

    closeButtonRef.current?.focus();
    const previousOverflow = document.body.style.overflow;
    document.body.style.overflow = 'hidden';
    return () => {
      document.body.style.overflow = previousOverflow;
    };
  }, [open]);

  if (!open) return null;

  return createPortal(
    <div
      ref={overlayRef}
      className={styles.overlay}
      role="dialog"
      aria-modal="true"
      aria-label={`原图预览：${alt}`}
      onClick={(event) => {
        if (event.target === event.currentTarget) onClose();
      }}
    >
      <div className={styles.toolbar}>
        <a
          className={styles.toolButton}
          href={src}
          target="_blank"
          rel="noreferrer"
          data-lightbox-focusable
        >
          <ExportOutlined />
          新窗口打开
        </a>
        <button
          ref={closeButtonRef}
          type="button"
          className={styles.toolButton}
          onClick={onClose}
          aria-label="关闭预览"
          data-lightbox-focusable
        >
          <CloseOutlined />
          关闭
        </button>
      </div>

      <img className={styles.image} src={src} alt={alt} />

      <div className={styles.hint} aria-hidden="true">
        点击空白处或按 ESC 关闭
      </div>
    </div>,
    document.body,
  );
}

export default ImageLightbox;
