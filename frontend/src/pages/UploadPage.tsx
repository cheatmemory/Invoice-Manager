import { useRef, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { Upload, message, List, Tag } from 'antd';
import { UploadOutlined, CheckCircleOutlined, CloseCircleOutlined } from '@ant-design/icons';
import type { UploadFile } from 'antd/es/upload/interface';
import { uploadInvoices } from '../services/api';
import type { UploadResponse } from '../types/invoice';
import styles from './UploadPage.module.css';

const { Dragger } = Upload;

function UploadPage() {
  const navigate = useNavigate();
  const [fileList, setFileList] = useState<UploadFile[]>([]);
  const [uploading, setUploading] = useState(false);
  const [results, setResults] = useState<UploadResponse[]>([]);
  /** 被拦下的文件的原因。用 ref 而不是普通数组：select 一批文件时会连着
   *  调很多次 beforeUpload，中途还会因为 setState 重渲染，普通变量会被重置。 */
  const rejectedRef = useRef<string[]>([]);

  const handleUpload = async () => {
    if (fileList.length === 0) {
      message.warning('请先选择文件');
      return;
    }

    setUploading(true);
    try {
      const files = fileList.map((f) => f.originFileObj as File);
      const response = await uploadInvoices(files);
      setResults(response);

      const ok = response.filter((r) => r.status === 'success').length;
      if (ok > 0) {
        message.success(`成功上传 ${ok} 个文件`);
        setFileList([]);
      }

      const bad = response.filter((r) => r.status === 'error').length;
      if (bad > 0) {
        message.error(`${bad} 个文件上传失败，详见下方结果`);
      }
    } catch (error) {
      // 失败提示由 axios 拦截器统一弹。
      console.error(error);
    } finally {
      setUploading(false);
    }
  };

  const successCount = results.filter((r) => r.status === 'success').length;
  const failCount = results.filter((r) => r.status === 'error').length;

  const uploadProps = {
    multiple: true,
    accept: '.pdf,.jpg,.jpeg,.png',
    fileList,
    beforeUpload: (file: File) => {
      const problems: string[] = [];

      if (!['application/pdf', 'image/jpeg', 'image/png'].includes(file.type)) {
        problems.push(`${file.name}：格式不支持，仅支持 PDF / JPG / PNG`);
      }
      if (file.size / 1024 / 1024 >= 10) {
        problems.push(`${file.name}：${(file.size / 1024 / 1024).toFixed(1)}MB，超过 10MB 上限`);
      }

      if (problems.length > 0) {
        // 攒着不弹：一次选 30 个文件、其中 5 个不合格，就会连着弹 5 条，
        // 前面几条瞬间被顶掉，用户根本看不清是哪个文件出了问题。
        rejectedRef.current.push(...problems);
        return Upload.LIST_IGNORE;
      }

      return false; // Prevent auto upload
    },
    onChange: ({ fileList: newFileList }: { fileList: UploadFile[] }) => {
      setFileList(newFileList);

      if (rejectedRef.current.length > 0) {
        message.error(
          `有 ${rejectedRef.current.length} 个文件没有加入上传列表：\n${rejectedRef.current.join('\n')}`,
        );
        rejectedRef.current = [];
      }
    },
    onRemove: (file: UploadFile) => {
      setFileList(fileList.filter((f) => f.uid !== file.uid));
    },
  };

  return (
    <div className={styles.pageContainer}>
      <div className={styles.contentWrapper}>
        <header className={styles.pageHeader}>
          <h1 className={styles.pageTitle}>上传发票</h1>
        </header>

        <div className={styles.uploadCard}>
          <div className={styles.uploadDropzone}>
            <Dragger {...uploadProps}>
              <div className="ant-upload-drag-icon">
                <span className={styles.uploadIcon}>
                  <UploadOutlined />
                </span>
              </div>
              <p className="ant-upload-text">点击或拖拽文件到此区域上传</p>
              <p className="ant-upload-hint">
                支持 PDF、JPG、PNG 格式，单个文件最大 10MB，支持批量上传
              </p>
            </Dragger>
          </div>

          <div className={styles.actionButtons}>
            <button
              className={styles.uploadButton}
              onClick={handleUpload}
              disabled={fileList.length === 0 || uploading}
            >
              {uploading ? '上传中...' : '开始上传'}
            </button>
            <button
              className={styles.backButton}
              onClick={() => navigate('/')}
            >
              返回列表
            </button>
          </div>
        </div>

        {results.length > 0 && (
          <div className={styles.resultsCard}>
            <div className={styles.resultsHeader}>
              <h2 className={styles.resultsTitle}>上传结果</h2>
              <span className={styles.resultsSummary}>
                {successCount > 0 && (
                  <span className={styles.resultOk}>成功 {successCount} 个</span>
                )}
                {failCount > 0 && (
                  <span className={styles.resultFail}>失败 {failCount} 个</span>
                )}
              </span>
            </div>
            {/* 一张都没成功时列表会是一整片红，先说清楚「文件没进去」，
                免得用户以为文件传上去了、只是解析失败。 */}
            {failCount > 0 && successCount === 0 && (
              <div className={styles.resultAllFailed}>
                上面的文件没有被导入，请修正后重新选择。
              </div>
            )}
            <List
              className={styles.resultsList}
              dataSource={results}
              renderItem={(item) => (
                <List.Item>
                  <List.Item.Meta
                    avatar={
                      item.status === 'success' ? (
                        <CheckCircleOutlined style={{ color: '#52c41a', fontSize: 24 }} />
                      ) : (
                        <CloseCircleOutlined style={{ color: '#ff4d4f', fontSize: 24 }} />
                      )
                    }
                    title={item.file_name}
                    description={item.message}
                  />
                  {item.status === 'success' && (
                    <Tag color="success">ID: {item.id}</Tag>
                  )}
                </List.Item>
              )}
            />
            <button
              className={styles.viewListButton}
              onClick={() => navigate('/')}
            >
              查看发票列表
            </button>
          </div>
        )}
      </div>
    </div>
  );
}

export default UploadPage;
