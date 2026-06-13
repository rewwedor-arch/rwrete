import sqlite3
import logging
import sys

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(message)s')
logger = logging.getLogger('train_model')

try:
    import pandas as pd
    from sklearn.model_selection import train_test_split
    from xgboost import XGBClassifier
    from sklearn.metrics import accuracy_score, precision_score, recall_score
    import joblib
except ImportError as e:
    print(f"\n[КРИТИЧЕСКАЯ ОШИБКА] Не установлена библиотека: {e}")
    print("Откройте терминал в вашей IDE (или командную строку) и введите команду:")
    print("pip install pandas scikit-learn joblib xgboost")
    input("\nНажмите Enter для выхода...")
    sys.exit(1)

def main():
    logger.info("1. Загрузка данных из БД...")
    conn = sqlite3.connect('smart_money.db')
    
    # Загружаем только закрытые сделки (result_pnl_pct is not null)
    df = pd.read_sql_query("SELECT * FROM ml_training_data WHERE result_pnl_pct IS NOT NULL", conn)
    conn.close()

    if len(df) == 0:
        logger.error("Нет данных для обучения (база пуста или нет закрытых сделок). Сначала соберите датасет!")
        return

    logger.info(f"Загружено сделок: {len(df)}")

    logger.info("2. Подготовка данных (Features & Target)...")
    # Преобразуем направление в число: LONG = 1, SHORT = 0
    df['side_binary'] = df['side'].apply(lambda x: 1 if x.upper() == 'LONG' else 0)

    # Определяем признаки (Features) — расширенный набор из 14 фич
    feature_columns = [
        'rsi', 'adx', 'ema200_dist_pct', 'order_book_imbalance', 'fear_greed_index', 'side_binary',
        'rsi_slope', 'adx_slope', 'atr_pct', 'volume_ratio',
        'ema50_dist_pct', 'bullish_candles_ratio', 'price_vs_equilibrium', 'macd_histogram'
    ]
    # Для старых БД без новых колонок — добавляем их с нулями
    for col in feature_columns:
        if col not in df.columns:
            df[col] = 0.0
    X = df[feature_columns].fillna(0) # На всякий случай заполняем NaN нулями

    # Формируем целевую переменную (Target)
    # 1 - хорошая сделка (чистый профит > 1.0% с учетом комиссий), 0 - убыточная или "копейки"
    y = (df['result_pnl_pct'] > 1.0).astype(int)

    logger.info("3. Разделение на обучающую и тестовую выборки (80/20)...")
    X_train, X_test, y_train, y_test = train_test_split(X, y, test_size=0.2, random_state=42)

    logger.info("4. Обучение градиентного бустинга (XGBoost)...")
    
    # Считаем баланс классов (обычно убыточных больше, чем прибыльных)
    pos_count = sum(y_train)
    neg_count = len(y_train) - pos_count
    scale_weight = neg_count / max(pos_count, 1)

    model = XGBClassifier(
        n_estimators=300, 
        max_depth=6, 
        learning_rate=0.03, 
        subsample=0.8,
        colsample_bytree=0.8,
        scale_pos_weight=scale_weight,
        eval_metric='logloss',
        random_state=42
    )
    model.fit(X_train, y_train)

    logger.info("5. Оценка качества модели на тестовой выборке:")
    y_prob = model.predict_proba(X_test)[:, 1]
    
    # Оценка для стандартного порога (Уверенность > 50%)
    y_pred_50 = (y_prob > 0.50).astype(int)
    acc_50 = accuracy_score(y_test, y_pred_50)
    prec_50 = precision_score(y_test, y_pred_50, zero_division=0)
    rec_50 = recall_score(y_test, y_pred_50, zero_division=0)
    
    # Оценка для высокого порога (Уверенность > 75%)
    y_pred_75 = (y_prob > 0.75).astype(int)
    prec_75 = precision_score(y_test, y_pred_75, zero_division=0)
    rec_75 = recall_score(y_test, y_pred_75, zero_division=0)

    logger.info(f"   [Порог входа > 50%]")
    logger.info(f"   ► Accuracy (Точность):  {acc_50*100:.2f}%")
    logger.info(f"   ► Precision (Доля верных сигналов): {prec_50*100:.2f}%")
    logger.info(f"   ► Recall (Полнота успешных сигналов): {rec_50*100:.2f}%\n")
    
    logger.info(f"   [Порог входа > 75% (Снайпер)]")
    logger.info(f"   ► Precision (Доля верных сигналов): {prec_75*100:.2f}%")
    logger.info(f"   ► Recall (Полнота успешных сигналов): {rec_75*100:.2f}%")

    logger.info("\nВажность признаков (Feature Importance):")
    importances = model.feature_importances_
    for col, imp in sorted(zip(feature_columns, importances), key=lambda x: x[1], reverse=True):
        logger.info(f"   • {col}: {imp*100:.2f}%")

    logger.info("\n6. Сохранение модели...")
    joblib.dump(model, 'trade_model.pkl')
    logger.info("Модель сохранена в 'trade_model.pkl'!")

if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        logger.error(f"Произошла критическая ошибка: {e}")
    finally:
        input("\nНажмите Enter для выхода...")
