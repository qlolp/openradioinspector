import { createRoot } from 'react-dom/client';
import { App } from './App';
import { connection } from './transport';
import './styles.css';

connection()
  .then((transport) =>
    createRoot(document.getElementById('root')!).render(<App transport={transport} />),
  )
  .catch(() => {
    document.getElementById('root')!.textContent =
      'The client could not initialize. Reload the page or open the legacy interface at /.';
  });
