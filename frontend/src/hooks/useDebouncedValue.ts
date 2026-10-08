import { useEffect, useState } from 'react';

/**
 * 一个值的防抖副本。
 *
 * 输入框要的是「立刻回显」的值，真正触发请求的是「停下来一会儿之后」的值 ——
 * 两份分开，输入框才不会卡住，请求也不会每敲一个字符就发一次。
 */
export function useDebouncedValue<T>(value: T, delay = 300): T {
  const [debounced, setDebounced] = useState(value);

  useEffect(() => {
    const timer = window.setTimeout(() => setDebounced(value), delay);
    // 依赖里带 value：下一次按键会清掉上一个定时器，只有最后一次按键活到超时。
    return () => window.clearTimeout(timer);
  }, [value, delay]);

  return debounced;
}

export default useDebouncedValue;
