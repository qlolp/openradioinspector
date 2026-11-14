import { createRoot } from 'react-dom/client';
import { ConnectionGate } from './ConnectionGate';
import { connection } from './transport';
import './styles.css';

connection()
  .then((transport) =>
    createRoot(document.getElementById('root')!).render(<ConnectionGate transport={transport} />),
  )
  .catch(() => {
    document.getElementById('root')!.textContent =
      'The client could not initialize. Reload the page or open the legacy interface at /.';
  });
