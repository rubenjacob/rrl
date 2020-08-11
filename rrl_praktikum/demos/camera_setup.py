from matplotlib import pyplot as plt

from rrl_praktikum.envs.kick_env import KickEnv

if __name__ == '__main__':
    env = KickEnv()

    obs, rewards, done, _ = env.step(env.action_space.sample())

    plt.imshow(obs['image'])
    plt.show()