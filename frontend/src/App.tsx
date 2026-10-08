import { useState, useEffect } from 'react';
import { BrowserRouter, Routes, Route } from 'react-router-dom';
import UploadPage from './pages/UploadPage';
import InvoiceListPage from './pages/InvoiceListPage';
import InvoiceDetailPage from './pages/InvoiceDetailPage';
import LLMConfigModal from './components/LLMConfigModal';
import { MainLayout } from './components/layout';
import { getLLMStatus } from './services/api';

function AppContent() {
  const [llmConfigOpen, setLlmConfigOpen] = useState(false);
  const [llmConfigured, setLlmConfigured] = useState<boolean | null>(null);
  const [showLlmPromo, setShowLlmPromo] = useState(false);

  // Check LLM status on app load
  useEffect(() => {
    const checkLLMStatus = async () => {
      try {
        const status = await getLLMStatus();
        setLlmConfigured(status.is_configured);
        // Show promotion banner if LLM is not configured (non-blocking)
        if (!status.is_configured) {
          setShowLlmPromo(true);
        }
      } catch (error) {
        // 请求失败说明是「连不上后端」，不是「LLM 没配」。这两件事混在一起会
        // 弹出一个「去配置 LLM」的推广条 —— 后端没起的时候照着它去配，
        // 配完照样用不了，反而把真正的问题盖住了。这里就不弹推广。
        // 失败原因由 axios 拦截器弹，不用再说一遍。
        console.error('Failed to check LLM status:', error);
        setLlmConfigured(null);
        setShowLlmPromo(false);
      }
    };
    checkLLMStatus();
  }, []);

  const handleLLMConfigured = () => {
    setLlmConfigured(true);
    setShowLlmPromo(false);
  };

  return (
    <MainLayout
      llmConfigured={llmConfigured}
      showLlmPromo={showLlmPromo}
      onOpenLLMConfig={() => setLlmConfigOpen(true)}
      onCloseLLMPromo={() => setShowLlmPromo(false)}
    >
      <Routes>
        <Route path="/" element={<InvoiceListPage />} />
        <Route path="/upload" element={<UploadPage />} />
        <Route path="/invoices/:id" element={<InvoiceDetailPage />} />
      </Routes>

      <LLMConfigModal
        open={llmConfigOpen}
        onClose={() => setLlmConfigOpen(false)}
        onConfigured={handleLLMConfigured}
      />
    </MainLayout>
  );
}

function App() {
  return (
    <BrowserRouter>
      <AppContent />
    </BrowserRouter>
  );
}

export default App;
