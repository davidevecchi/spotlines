import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from matplotlib.colors import to_rgb

csv = '../backend/feature_map.csv'


def main():
    df = pd.read_csv(csv)
    
    # NEW: group rows by color
    grouped = df.groupby('color')
    
    colors = list(grouped.groups.keys())
    
    # OLD:
    # rgb = np.array([[to_rgb(c)] for c in colors])
    
    # NEW:
    plt.figure(figsize=(16, len(colors) * 0.6))
    
    # NEW: build readable labels
    labels = []
    
    for color, group in grouped:
        keys = sorted(set([row['key'] for _, row in group.iterrows()]))
        
        labels.append(", ".join(keys) + ": "  + color)
    
    combo = sorted(zip(labels, colors))
    for l, _ in combo:
        print(l)
    colors = [c for _, c in combo]
    labels = [l for l, _ in combo]
    
    rgb = np.array([[to_rgb(c)] for c in colors])  # shape: (N, 1, 3)
    
    plt.imshow(rgb)
    
    # NEW:
    plt.yticks(
        ticks=np.arange(len(colors)),
        labels=labels,
        fontsize=16,
    )
    
    plt.xticks([])
    
    # NEW: remove frame clutter
    for spine in plt.gca().spines.values():
        spine.set_visible(False)
    
    # plt.tight_layout()
    plt.show()


if __name__ == '__main__':
    main()
